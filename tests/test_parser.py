from decimal import Decimal

from chollometro_alerts.parser import parse_search

HTML = """<article id="thread_123"><div class="threadListCard-header"><span>Publicado hace 2 h</span><button title="Actualmente tiene 268°.">268°</button></div><a class="thread-title" href="https://x/ofertas/cerveza-123">Pack cerveza Mahou</a><span class="thread-price">12,50€</span><a data-t="merchantLink">Carrefour</a></article>"""


def test_parse_deal():
    d = parse_search(HTML, "cerveza")[0]
    assert d.deal_id == "123"
    assert d.title == "Pack cerveza Mahou"
    assert d.price == Decimal("12.50")
    assert d.merchant == "Carrefour"
    assert d.temperature == 268
    assert d.category == "beer"
    assert d.published_at
