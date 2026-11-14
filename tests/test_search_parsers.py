"""Search-engine page parsers and the fallback order, without the network. Run: python -m tests.test_search_parsers"""
import asyncio

import httpx

from backend import web

LITE = """<a rel="nofollow" href="https://a.example/x" class='result-link'>A &amp; B</a>
<td class='result-snippet'>First <b>snippet</b></td>"""
YAHOO = """<div class="algo-sr"><a href="https://r.search.yahoo.com/_ylt=x/RV=2/RU=https%3a%2f%2fwww.f1.com%2fresults/RK=2/RS=y">
<h3 class="title">2026 RACE RESULTS</h3></a><div class="compText aAbs"><p>Norris wins</p></div></div>"""
CHALLENGE = "Unfortunately, bots use DuckDuckGo too. Please complete the following challenge."


def main():
    assert web._parse_lite(LITE) == [{"title": "A & B", "url": "https://a.example/x", "snippet": "First snippet"}]
    assert web._parse_yahoo(YAHOO) == [{"title": "2026 RACE RESULTS", "url": "https://www.f1.com/results", "snippet": "Norris wins"}]

    def handler(request):  # both DuckDuckGo pages answer with the puzzle; Yahoo answers properly
        return httpx.Response(200, text=YAHOO if "yahoo" in request.url.host else CHALLENGE)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await web.search(client, "f1")
    assert asyncio.run(run())[0]["url"] == "https://www.f1.com/results"
    print("search parsers OK")


if __name__ == "__main__":
    main()
