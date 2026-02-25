from rest_framework import permissions


class IsOwner(permissions.BasePermission):
    message = "You do not have permission to access this resource."

    def has_object_permission(self, request, view, obj):
        # Handle different model types gracefully
        if hasattr(obj, 'user'):
            return obj.user == request.user
        if hasattr(obj, 'playlist'):
            return obj.playlist.user == request.user
        return False  # deny by default if no owner field