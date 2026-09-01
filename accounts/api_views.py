"""
Session-based authentication API.

The dashboard's JavaScript talks to these endpoints, so they use the same
session cookie and CSRF protection as the HTML pages rather than a second,
token-based auth scheme.
"""

from __future__ import annotations

from django.contrib.auth import login as django_login
from django.contrib.auth import logout as django_logout
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.serializers import LoginSerializer, UserSerializer


class LoginAPIView(APIView):
    """POST credentials, receive the authenticated user and a session cookie."""

    permission_classes = [AllowAny]
    throttle_scope = "login"
    serializer_class = LoginSerializer

    @extend_schema(
        request=LoginSerializer,
        responses={200: UserSerializer},
        description="Authenticate and start a session.",
    )
    def post(self, request: Request) -> Response:
        serializer = LoginSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)

        user = serializer.validated_data["user"]
        # Rotates the session key, defeating session fixation.
        django_login(request, user)

        return Response(UserSerializer(user).data, status=status.HTTP_200_OK)


class LogoutAPIView(APIView):
    """POST to end the session. GET is not accepted, to avoid drive-by logout."""

    permission_classes = [IsAuthenticated]

    @extend_schema(request=None, responses={204: None}, description="End the current session.")
    def post(self, request: Request) -> Response:
        django_logout(request)
        return Response(status=status.HTTP_204_NO_CONTENT)


class CurrentUserAPIView(APIView):
    """The signed-in user and their capabilities, for bootstrapping the UI."""

    permission_classes = [IsAuthenticated]

    @extend_schema(responses={200: UserSerializer}, description="Return the current user.")
    def get(self, request: Request) -> Response:
        return Response(UserSerializer(request.user).data)
