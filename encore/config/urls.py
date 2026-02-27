from django.contrib import admin
from django.urls import path, include
from apps.users.views import RegisterView
from rest_framework.routers import DefaultRouter 
from apps.playlists.views import PlaylistViewSet, SyncOperationDetailView

from rest_framework_simplejwt.views import (
    TokenObtainPairView,
    TokenRefreshView,
)

router = DefaultRouter() 
router.register(r'playlists', PlaylistViewSet, basename='playlist')


urlpatterns = [
    path('admin/', admin.site.urls),
    
    # JWT endpoints
    path('api/token/', TokenObtainPairView.as_view(), name='token_obtain_pair'),      # login
    path('api/token/refresh/', TokenRefreshView.as_view(), name='token_refresh'),     # refresh access token
    path('api/register/', RegisterView.as_view(), name='register'),
    
    path('api/', include(router.urls)), 
    path('api/sync-operations/<int:pk>/', SyncOperationDetailView.as_view(), name='sync-operation-detail'),
] 