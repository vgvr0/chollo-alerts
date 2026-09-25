from dataclasses import dataclass


@dataclass(frozen=True)
class TelegramIdentity:
    telegram_user_id: str
    telegram_chat_id: str
    username: str | None
    first_name: str | None


class TelegramUserResolver:
    """Resolve a Telegram update without making the parser an auth boundary."""

    def __init__(
        self, repository, *, enabled=False, auto_register=False, legacy_chat_id=None
    ):
        self.repository = repository
        self.enabled = enabled
        self.auto_register = auto_register
        self.legacy_chat_id = (
            str(legacy_chat_id) if legacy_chat_id is not None else None
        )

    @staticmethod
    def is_allowed_chat(update, *, allow_missing=False):
        """Private chats only; unknown Telegram chat types are denied."""
        message = update.get("message") or {}
        chat = message.get("chat") or {}
        chat_type = chat.get("type")
        return chat_type == "private" or (allow_missing and chat_type is None)

    @staticmethod
    def identity_from_update(update):
        message = update.get("message") or {}
        sender = message.get("from") or {}
        chat = message.get("chat") or {}
        user_id = sender.get("id")
        chat_id = chat.get("id")
        if user_id is None or chat_id is None:
            return None
        return TelegramIdentity(
            str(user_id), str(chat_id), sender.get("username"), sender.get("first_name")
        )

    def resolve(self, update):
        if not self.is_allowed_chat(update, allow_missing=not self.enabled):
            return None
        identity = self.identity_from_update(update)
        if identity is None:
            if not self.enabled:
                message = update.get("message") or {}
                chat_id = (message.get("chat") or {}).get("id")
                if (
                    self.legacy_chat_id is not None
                    and str(chat_id) != self.legacy_chat_id
                ):
                    return None
                return (
                    self.repository.user_for_chat(chat_id)
                    or self.repository.legacy_user()
                    or self.repository.create_user(
                        telegram_chat_id=chat_id, username="legacy/default"
                    )
                )
            return None
        if not self.enabled:
            if (
                self.legacy_chat_id is not None
                and identity.telegram_chat_id != self.legacy_chat_id
            ):
                return None
            return (
                self.repository.user_for_chat(identity.telegram_chat_id)
                or self.repository.legacy_user()
                or self.repository.create_user(
                    telegram_chat_id=identity.telegram_chat_id,
                    username="legacy/default",
                )
            )
        user = self.repository.reconcile_telegram_identity(identity)
        if user is not None:
            if not user.enabled:
                return None
            return user
        if not self.auto_register:
            return None
        return self.repository.create_user(
            telegram_user_id=identity.telegram_user_id,
            telegram_chat_id=identity.telegram_chat_id,
            username=identity.username,
            first_name=identity.first_name,
        )
