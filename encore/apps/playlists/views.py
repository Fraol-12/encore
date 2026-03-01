import math

from celery import current_app
from django.db import IntegrityError
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.generics import RetrieveAPIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.users.permissions import IsOwner

from .models import Playlist, SyncOperation
from .serializers import PlaylistItemSerializer, PlaylistSerializer, SyncOperationSerializer
from .services.youtube_service import YouTubeService
from .tasks import process_sync


class SyncOperationDetailView(RetrieveAPIView):
    queryset = SyncOperation.objects.all()
    serializer_class = SyncOperationSerializer
    permission_classes = [IsAuthenticated, IsOwner]

    def get_queryset(self):
        return SyncOperation.objects.filter(playlist__user=self.request.user)


class PlaylistViewSet(viewsets.ModelViewSet):
    queryset = Playlist.objects.all()
    serializer_class = PlaylistSerializer
    permission_classes = [IsAuthenticated, IsOwner]

    def get_queryset(self):
        return self.queryset.filter(user=self.request.user)

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        youtube_id = serializer.validated_data.get("youtube_playlist_id")
        if not youtube_id:
            return Response(
                {"youtube_playlist_id": ["This field is required."]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            service = YouTubeService()
            playlist = service.create_from_youtube(user=request.user, youtube_playlist_id=youtube_id)
            return Response(self.get_serializer(playlist).data, status=status.HTTP_201_CREATED)
        except Exception as exc:  # noqa: BLE001
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    def _get_rate_limit_cooldown_for_user(self, user):
        recent_failed = (
            SyncOperation.objects.filter(playlist__user=user, status="failed")
            .order_by("-ended_at", "-id")[:20]
        )

        max_remaining = 0
        source_operation_id = None
        source_playlist_id = None

        now = timezone.now()
        for operation in recent_failed:
            if not isinstance(operation.errors, dict):
                continue
            summary = operation.errors.get("summary")
            if not isinstance(summary, dict) or not summary.get("rate_limited"):
                continue

            retry_after = int(summary.get("retry_after_seconds") or 0)
            if retry_after <= 0 or not operation.ended_at:
                continue

            elapsed = (now - operation.ended_at).total_seconds()
            remaining = int(math.ceil(retry_after - elapsed))
            if remaining > max_remaining:
                max_remaining = remaining
                source_operation_id = operation.id
                source_playlist_id = operation.playlist_id

        return max_remaining, source_operation_id, source_playlist_id

    @action(detail=True, methods=["post"], url_path="sync")
    def sync(self, request, pk=None):
        playlist = self.get_object()

        user_remaining, user_op_id, user_playlist_id = self._get_rate_limit_cooldown_for_user(request.user)
        if user_remaining > 0:
            return Response(
                {
                    "detail": "Spotify is rate-limiting requests for this account. Retry later.",
                    "retry_after_seconds": user_remaining,
                    "last_sync_operation_id": user_op_id,
                    "last_rate_limited_playlist_id": user_playlist_id,
                    "scope": "account",
                },
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        last_failed = (
            SyncOperation.objects.filter(playlist=playlist, status="failed")
            .order_by("-id")
            .first()
        )
        if last_failed and isinstance(last_failed.errors, dict):
            summary = last_failed.errors.get("summary") or {}
            if isinstance(summary, dict) and summary.get("rate_limited"):
                retry_after = int(summary.get("retry_after_seconds") or 0)
                ended_at = last_failed.ended_at
                if retry_after > 0 and ended_at:
                    elapsed = (timezone.now() - ended_at).total_seconds()
                    remaining = int(math.ceil(retry_after - elapsed))
                    if remaining > 0:
                        return Response(
                            {
                                "detail": "Spotify is rate-limiting requests. Retry later.",
                                "retry_after_seconds": remaining,
                                "last_sync_operation_id": last_failed.id,
                                "scope": "playlist",
                            },
                            status=status.HTTP_429_TOO_MANY_REQUESTS,
                        )

        active = SyncOperation.objects.filter(
            playlist=playlist,
            status__in=["queued", "running"],
        )

        if active.exists():
            active_ids = list(active.values_list("id", flat=True))
            return Response(
                {"detail": f"Sync already queued/running (IDs: {active_ids})"},
                status=status.HTTP_409_CONFLICT,
            )

        try:
            operation = SyncOperation.objects.create(
                playlist=playlist,
                status="queued",
                triggered_by="user",
            )
        except IntegrityError:
            return Response(
                {"detail": "A sync is already queued or running for this playlist."},
                status=status.HTTP_409_CONFLICT,
            )

        async_result = process_sync.delay(operation.id)
        operation.errors = {"task_id": async_result.id}
        operation.save(update_fields=["errors"])

        playlist.sync_status = "queued"
        playlist.save(update_fields=["sync_status", "updated_at"])

        return Response(
            {
                "status": "queued",
                "sync_operation_id": operation.id,
                "message": f"Sync queued. Poll /api/sync-operations/{operation.id}/",
            },
            status=status.HTTP_202_ACCEPTED,
        )

    @action(detail=True, methods=["post"], url_path="cancel-sync")
    def cancel_sync(self, request, pk=None):
        playlist = self.get_object()
        operation = (
            SyncOperation.objects.filter(playlist=playlist, status__in=["queued", "running"])
            .order_by("-id")
            .first()
        )

        if not operation:
            return Response(
                {"detail": "No active sync operation to cancel."},
                status=status.HTTP_404_NOT_FOUND,
            )

        existing_errors = operation.errors if isinstance(operation.errors, dict) else {}
        task_id = existing_errors.get("task_id")
        if task_id:
            current_app.control.revoke(task_id, terminate=True, signal="SIGTERM")

        operation.status = "failed"
        operation.ended_at = timezone.now()
        operation.error_count = max(operation.error_count, 1)
        operation.errors = {
            **existing_errors,
            "cancelled": True,
            "cancelled_by": "user",
            "cancelled_at": operation.ended_at.isoformat(),
            "error": "Sync cancelled by user request",
        }
        operation.save(update_fields=["status", "ended_at", "error_count", "errors"])

        playlist.sync_status = "failed"
        playlist.save(update_fields=["sync_status", "updated_at"])

        return Response(
            {"detail": f"Sync operation {operation.id} cancelled."},
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["get"], url_path="items")
    def items(self, request, pk=None):
        playlist = self.get_object()
        items = playlist.items.order_by("position")
        serializer = PlaylistItemSerializer(items, many=True)
        return Response(serializer.data)
