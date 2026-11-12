"""
collectors/alerts.py — optional Telegram messages: a new deal, or a watcher
that keeps failing.

Off until a bot token and chat id are set, either as environment variables
(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID) or in data/secrets.toml:

    [telegram]
    bot_token = "123456:ABC..."     # from @BotFather
    chat_id = "123456789"           # your chat with the bot

data/secrets.toml is kept out of git; never put the token in config.toml,
which is in git. What gets sent is chosen in config.toml [alerts].
"""

import os
import sys
import tomllib

import requests

from settings import SETTINGS, project_path

SECRETS_PATH = project_path(SETTINGS["paths"]["data_dir"]) / "secrets.toml"


def telegram_config(secrets_path=SECRETS_PATH):
    """(bot_token, chat_id), or None when alerts aren't set up."""
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        try:
            with open(secrets_path, "rb") as fh:
                tg = tomllib.load(fh).get("telegram", {})
            token, chat = tg.get("bot_token"), tg.get("chat_id")
        except (OSError, tomllib.TOMLDecodeError):
            return None
    return (str(token), str(chat)) if token and chat else None


def send(text, config=None, post=None):
    """Send one message; True if Telegram accepted it. Never raises: a failed
    alert must not stop the watcher."""
    config = config if config is not None else telegram_config()
    if not config:
        return False
    token, chat = config
    post = post or requests.post
    try:
        r = post(f"https://api.telegram.org/bot{token}/sendMessage",
                 json={"chat_id": chat, "text": text, "disable_web_page_preview": True},
                 timeout=10)
        if r.status_code != 200:
            print(f"  ! Telegram alert refused: HTTP {r.status_code}", file=sys.stderr)
            return False
        return True
    except requests.RequestException as exc:
        print(f"  ! Telegram alert failed: {exc.__class__.__name__}", file=sys.stderr)
        return False


def deal_message(source, title, year, price, mid, lo, link, also=None, similar=None,
                 verdict="deal"):
    """`similar`: (similar cars in the database, how many are cheaper, their median).
    A "maybe" (too few similar cars to confirm) is announced as a possible deal."""
    under = round((lo - price) / lo * 100) if lo else 0
    what = "Deal" if verdict == "deal" else "Possible deal"
    text = (f"{what} on {source}: {title} {year or ''} for €{price:,}\n"
            f"Typical asking €{mid:,.0f}; {under}% under the low end of its range.\n")
    if similar and similar[0]:
        n, cheaper, median = similar
        text += f"Cheaper than {n - cheaper} of {n} similar cars (their median €{median:,}).\n"
    text += link
    if also:
        text += f"\nAlso on {also['source']} for €{also['price_eur']:,}: {also['link']}"
    return text


def deal_alert(source, title, year, price, mid, lo, link, also=None, similar=None,
               verdict="deal"):
    if SETTINGS["alerts"]["deals"]:
        send(deal_message(source, title, year, price, mid, lo, link, also, similar, verdict))


def failure_alert(source, failures):
    if SETTINGS["alerts"]["watcher_failures"]:
        send(f"The {source} watcher has failed {failures} scans in a row. "
             "The site may be refusing requests; check the dashboard.")
