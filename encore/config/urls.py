from django.contrib import admin
from django.urls import path, include
from apps.users.views import RegisterView
from rest_framework.routers import DefaultRouter 
from apps.playlists.views import PlaylistViewSet, SyncOperationDetailView
from apps.spotify.views import SpotifyLoginView, SpotifyCallbackView, SpotifyStatusView 
from rest_framework_simplejwt.views import (
    TokenObtainPairView,
    TokenRefreshView,
)
from rest_framework.schemas import get_schema_view
from django.views.generic import TemplateView
from django.views.generic.base import RedirectView

schema_view = get_schema_view(
    title="Encore API",
    description="YouTube -> Spotify playlist sync API",
    version="1.0.0",
)

router = DefaultRouter() 
router.register(r'playlists', PlaylistViewSet, basename='playlist')


urlpatterns = [
    path('admin/', admin.site.urls),
    path('', RedirectView.as_view(url='/encore/', permanent=False), name='home-redirect'),
    path('encore/', TemplateView.as_view(template_name='encore/index.html'), name='encore-frontend'),
    
    # JWT endpoints
    path('api/token/', TokenObtainPairView.as_view(), name='token_obtain_pair'),      # login
    path('api/token/refresh/', TokenRefreshView.as_view(), name='token_refresh'),     # refresh access token
    path('api/register/', RegisterView.as_view(), name='register'),
    
    path('api/', include(router.urls)), 
    path("api/schema/", schema_view, name="api-schema"),
    path("api-auth/", include("rest_framework.urls")),
    path('api/sync-operations/<int:pk>/', SyncOperationDetailView.as_view(), name='sync-operation-detail'),
    path('api/spotify/login/', SpotifyLoginView.as_view(), name='spotify-login'),
    path('api/spotify/callback/', SpotifyCallbackView.as_view(), name='spotify-callback'),
    path('api/spotify/status/', SpotifyStatusView.as_view(), name='spotify-status'),
] 
