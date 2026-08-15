"""Keyboard builders supporting Bot API 9.4 inline button styles (`style` field).

Telegram added the `style` field to InlineKeyboardButton and KeyboardButton in
Bot API 9.4 (2026-02-09). Library support is uneven, so we patch the markup
dictionaries directly in `as_telegram_dict` before sending.

Allowed style values seen in the wild: 'success' | 'danger' | 'primary'.
We expose a friendly mapping: green→success, red→danger, blue→primary.
"""
from __future__ import annotations

from dataclasses import dataclass

from .locales import t

COLOR_TO_STYLE = {
    "green": "success",
    "red": "danger",
    "blue": "primary",
}


@dataclass
class Btn:
    text: str
    callback_data: str | None = None
    url: str | None = None
    web_app_url: str | None = None
    color: str | None = None  # 'green' | 'red' | 'blue' | None
    icon_custom_emoji_id: str | None = None  # Bot API 9.4

    def as_dict(self) -> dict:
        d: dict = {"text": self.text}
        if self.callback_data is not None:
            d["callback_data"] = self.callback_data
        if self.url is not None:
            d["url"] = self.url
        if self.web_app_url is not None:
            d["web_app"] = {"url": self.web_app_url}
        style = COLOR_TO_STYLE.get(self.color) if self.color else None
        if style:
            d["style"] = style
        if self.icon_custom_emoji_id:
            d["icon_custom_emoji_id"] = self.icon_custom_emoji_id
        return d


def inline_kb(rows: list[list[Btn]]) -> dict:
    return {"inline_keyboard": [[b.as_dict() for b in row] for row in rows]}


def reply_kb(rows: list[list[dict]], *, resize: bool = True, one_time: bool = False) -> dict:
    return {
        "keyboard": rows,
        "resize_keyboard": resize,
        "one_time_keyboard": one_time,
    }


# ---------- Pre-built keyboards ---------- #

def kb_share_contact(lang: str) -> dict:
    return reply_kb([[{"text": t(lang, "share_contact_btn"), "request_contact": True}]], one_time=True)


def kb_force_sub(channels: list[tuple[int, str | None, str | None]], lang: str) -> dict:
    """`channels`: list of (chat_id, title, invite_link)."""
    rows: list[list[Btn]] = []
    for chat_id, title, link in channels:
        if link:
            rows.append([Btn(text=f"{t(lang, 'force_sub_subscribe_btn')} {title or chat_id}",
                             url=link, color="blue")])
    rows.append([Btn(text=t(lang, "force_sub_check_btn"), callback_data="check_force_sub", color="green")])
    return inline_kb(rows)


def kb_main_menu(lang: str, *, is_admin: bool, web_app_url: str | None) -> dict:
    """Tri-colour main menu (Bot API 9.4 ``style`` field).

    Layout uses three colours so the menu reads at a glance:
      * green/success  — primary actions: upload + open Mini App
      * blue/primary   — informational actions: points, invite, my bots
      * red/danger     — power actions: admin panel
    """
    rows: list[list[Btn]] = [
        [
            Btn(text=t(lang, "btn_upload"), callback_data="upload", color="green"),
        ],
    ]
    if web_app_url:
        rows.append([
            Btn(text=t(lang, "btn_open_app"), web_app_url=web_app_url, color="green"),
        ])
    # The MCV assistant entry point lives on its own row in red so it
    # screams "ask me to write or edit a bot for you" — exactly the
    # action the user wants to surface most prominently.
    rows.append([Btn(text=t(lang, "btn_mcv"), callback_data="mcv", color="red")])
    # Public REST API entry — same colour, screams "developer goodies".
    rows.append([Btn(text=t(lang, "btn_api"), callback_data="api", color="red")])
    rows.extend([
        [
            Btn(text=t(lang, "btn_my_bots"), callback_data="my_bots", color="blue"),
            Btn(text=t(lang, "btn_points"), callback_data="points", color="blue"),
        ],
        [
            Btn(text=t(lang, "btn_invite"), callback_data="invite", color="blue"),
            Btn(text=t(lang, "btn_developer"), callback_data="developer", color="blue"),
        ],
    ])
    if is_admin:
        rows.append([Btn(text=t(lang, "btn_admin"), callback_data="admin", color="red")])
    return inline_kb(rows)


def kb_back_main(lang: str) -> dict:
    return inline_kb([[Btn(text=t(lang, "btn_main"), callback_data="main", color="blue")]])
