from django.contrib import admin
from django.urls import path, include
from apps.users.views import RegisterView
from rest_framework_simplejwt.views import (
    TokenObtainPairView,
    TokenRefreshView,
)

urlpatterns = [
    path('admin/', admin.site.urls),
    
    # JWT endpoints
    path('api/token/', TokenObtainPairView.as_view(), name='token_obtain_pair'),      # login
    path('api/token/refresh/', TokenRefreshView.as_view(), name='token_refresh'),     # refresh access token
    path('api/register/', RegisterView.as_view(), name='register'),
    
    # We'll add register later
]