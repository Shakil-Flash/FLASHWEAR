from django.contrib.auth import backends, get_user_model

UserModel = get_user_model()


class EmailBackend(backends.ModelBackend):
    """Authenticate against ``User.email`` (case-insensitive)."""

    def authenticate(self, request, username=None, password=None, **kwargs):
        email = kwargs.get("email") or username
        if email is None or password is None:
            return None
        try:
            user = UserModel._default_manager.get(email__iexact=email)
        except UserModel.DoesNotExist:
            UserModel().set_password(password)
            return None
        except UserModel.MultipleObjectsReturned:
            # In practice the unique constraint prevents this; fall back to the first.
            user = UserModel._default_manager.filter(email__iexact=email).first()
            if user is None:
                return None
        if user.check_password(password) and self.user_can_authenticate(user):
            return user
        return None
