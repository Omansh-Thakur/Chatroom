import re
from datetime import datetime


class RuleBasedAI:
    def __init__(self, bot_name="LocalAI"):
        self.bot_name = bot_name
        self.bot_name_lower = bot_name.lower()

    def _help_text(self):
        return (
            "I am a local rule based bot. Try: '/ai hello', '/ai time', "
            "'/ai date', or '/ai rules'."
        )

    def _normalize(self, text):
        return " ".join(text.split())

    def _strip_prefix(self, message):
        lowered = message.lower()

        if lowered.startswith("@localai"):
            return message[len("@localai") :].strip(), True

        if lowered.startswith(f"@{self.bot_name_lower}"):
            return message[len(self.bot_name) + 1 :].strip(), True

        if lowered.startswith("/ai"):
            return message[3:].strip(), True

        return message, False

    def generate_reply(self, sender_nickname, message_text):
        if not message_text:
            return None

        sender = sender_nickname.strip().lower()
        if sender == self.bot_name_lower:
            return None

        message = self._normalize(message_text.strip())
        if not message:
            return None

        stripped, direct_request = self._strip_prefix(message)
        lowered = stripped.lower()

        if not stripped and direct_request:
            return self._help_text()

        if "rules" in lowered or "help" in lowered:
            return self._help_text()

        if re.search(r"\b(hii|hello|hey|namaste)\b", lowered):
            return f"Hello {sender_nickname}! type '/ai help' to see commands."

        if "time" in lowered:
            now = datetime.now().strftime("%I:%M %p")
            return f"time is {now}."

        if "date" in lowered or "day" in lowered:
            today = datetime.now().strftime("%A, %d %B %Y")
            return f"Today's date is {today}."

        if re.search(r"\b(thanks|thank you)\b", lowered):
            return "You're welcome."

        if re.search(r"\b(bye|goodbye|see you)\b", lowered):
            return "Goodbye."

        if direct_request:
            return (
                "I do not have a rule for that yet."
            )

        return None
