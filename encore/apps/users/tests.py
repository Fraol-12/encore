from django.urls import reverse
from rest_framework.test import APITestCase

from apps.users.models import CustomUser


class RegisterViewTests(APITestCase):
    def test_register_user(self):
        response = self.client.post(
            reverse("register"),
            {"email": "new@example.com", "password": "strongpass123"},
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertTrue(CustomUser.objects.filter(email="new@example.com").exists())
