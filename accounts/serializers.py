"""Serializers for the authentication API."""

from __future__ import annotations

from django.contrib.auth import authenticate, get_user_model
from rest_framework import serializers

User = get_user_model()


class UserSerializer(serializers.ModelSerializer):
    """Public representation of a user. Never includes the password hash."""

    display_name = serializers.CharField(read_only=True)
    initials = serializers.CharField(read_only=True)
    capabilities = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = (
            "id",
            "email",
            "first_name",
            "last_name",
            "display_name",
            "initials",
            "role",
            "is_active",
            "last_login",
            "date_joined",
            "capabilities",
        )
        read_only_fields = fields

    def get_capabilities(self, obj) -> dict[str, bool]:
        return {
            "is_administrator": obj.is_administrator,
            "can_manage_contacts": obj.can_manage_contacts,
            "can_manage_campaigns": obj.can_manage_campaigns,
            "can_launch_campaigns": obj.can_launch_campaigns,
        }


class LoginSerializer(serializers.Serializer):
    """Validates credentials without revealing which half was wrong."""

    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, style={"input_type": "password"})

    default_error_messages = {
        "invalid_credentials": "Incorrect email address or password.",
        "inactive": "This account has been deactivated.",
    }

    def validate(self, attrs: dict) -> dict:
        request = self.context.get("request")
        user = authenticate(
            request=request,
            username=attrs["email"].lower(),
            password=attrs["password"],
        )
        if user is None:
            self.fail("invalid_credentials")
        if not user.is_active:
            self.fail("inactive")

        attrs["user"] = user
        return attrs
