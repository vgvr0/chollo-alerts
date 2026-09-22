import json
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

from bs4 import BeautifulSoup

from .filters import category_for
from .models import Deal


def _price(text):
    if not text:
        return None
    m = re.search(r"(\d+[,.]\d{1,2})\s*€", text)
    if not m:
        return None
    try:
        return Decimal(m.group(1).replace(",", "."))
    except InvalidOperation:
        return None


def _published(text):
    if not text:
        return None
    m = re.search(
        r"publicado hace\s+(\d+)\s+(min|h|día|días|semana|semanas)", text.casefold()
    )
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2)
    seconds = (
        n
        * (
            {
                "min": 60,
                "h": 3600,
                "día": 86400,
                "días": 86400,
                "semana": 604800,
                "semanas": 604800,
            }[unit]
        )
    )
    return datetime.now(UTC) - timedelta(seconds=seconds)


def parse_search(html: str, query: str) -> list[Deal]:
    soup = BeautifulSoup(html, "html.parser")
    deals = []
    for article in soup.select('article[id^="thread_"]'):
        aid = article.get("id", "").removeprefix("thread_")
        link = article.select_one("a.thread-title, a.thread-title--list")
        if not aid or not link:
            continue
        data = {}
        vue = article.select_one("[data-vue3]")
        if vue:
            try:
                props = json.loads(vue.get("data-vue3", "{}")).get("props", {})
                data = props.get("thread", {})
            except (TypeError, json.JSONDecodeError):
                pass
        title = data.get("title") or link.get_text(" ", strip=True)
        description = article.get_text(" ", strip=True)
        category = category_for(title, query)
        if (
            query.casefold() in {"leche", "cerveza", "cervezas"}
            and category == "generic"
        ):
            continue
        if not category:
            continue
        temp = None
        if data.get("temperature") is not None:
            temp = round(float(data["temperature"]))
        else:
            mt = re.search(r"([+-]?\d+)°", article.get_text(" ", strip=True))
            if mt:
                temp = int(mt.group(1))
        merchant = None
        mm = article.select_one('[data-t="merchantLink"]')
        if mm:
            merchant = mm.get_text(" ", strip=True)
        elif data.get("merchant"):
            merchant = data["merchant"].get("merchantName")
        price = _price(
            article.select_one(".thread-price").get_text(" ", strip=True)
            if article.select_one(".thread-price")
            else ""
        )
        if price is None and data.get("price") is not None:
            price = Decimal(str(data["price"]))
        stamp = article.select_one(".threadListCard-header")
        url = link.get("href", "")
        if url.startswith("/"):
            url = "https://www.chollometro.com" + url
        published = _published(stamp.get_text(" ", strip=True) if stamp else "")
        if published is None and data.get("publishedAt"):
            published = datetime.fromtimestamp(data["publishedAt"], UTC)
        metadata = " ".join(
            str(value)
            for key, value in data.items()
            if isinstance(value, (str, int, float))
            and key.casefold() not in {"title", "price"}
        )
        product_text = " ".join(filter(None, (title, description, merchant, metadata)))
        deals.append(
            Deal(
                aid,
                title,
                url,
                price,
                merchant,
                temp,
                category,
                published,
                product_text=product_text,
                description=description,
                original_price=None,
                image=(
                    article.select_one("img").get("src")
                    if article.select_one("img")
                    else None
                ),
                source_query=query,
            )
        )
    return deals
