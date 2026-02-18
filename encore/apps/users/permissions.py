from rest_framework import permissions


class IsOwner(permissions.BasePermission):
    """
    Custom permission to only allow owners of an object to view/edit it.
    Assumes the model has a 'user' ForeignKey to the authenticated user model.
    """
    message = "You do not have permission to access this resource."

    def has_object_permission(self, request, view, obj):
        # Instance must have an attribute named `user`.
        return obj.user == request.user