import asyncio
import json
import re
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from functools import cache
from pathlib import Path
from urllib.parse import urlparse

import dateparser
import httpx
import imdb
from bs4 import BeautifulSoup
from cinemagoerng import web

BASE_URL = "https://www.cinelibri.com/filmi-2025/"

CLIENT = httpx.AsyncClient()
IA = imdb.Cinemagoer()
TOKEN = "/*__EMBED_FILMS_JSON__*/"


@dataclass
class Screening:
    datetime: datetime
    location: str


@dataclass
class ImdbData:
    imdb_url: str
    imdb_rating: float | None


@dataclass
class Film:
    id: str
    title: str
    original_title: str
    year: int | None
    genres: str
    language: str
    url: str
    screenings: list[Screening]
    imdb: ImdbData | None = None


@dataclass
class ImdbCuration:
    id: str
    imdb_id: str
    imdb_score: float | None = None


@cache
def load_curations(curations_path="curations.json") -> dict[str, ImdbCuration]:
    with open(curations_path, "r") as f:
        curations_json = json.load(f)

    curations = []
    for curation in curations_json["curations"]:
        curations.append(ImdbCuration(**curation))

    return {curation.id: curation for curation in curations}


def serialize_films(films: list[Film]) -> str:
    out = []
    for f in films:
        d = asdict(f)
        d["screenings"] = [
            {"datetime": s.datetime.isoformat(), "location": s.location}
            for s in f.screenings
        ]
        out.append(d)
    return json.dumps(out, ensure_ascii=False)


def generate_index(
    films: list[Film],
    template_path: str = "html_template.html",
    out_path: str = "index.html",
):
    tpl_path = Path(template_path)
    if not tpl_path.exists():
        print(f"Template file not found: {tpl_path.resolve()}")
        print(
            "Make sure html_template.html.in is in the same directory as this script."
        )
        sys.exit(2)

    tpl = tpl_path.read_text(encoding="utf-8")
    if TOKEN not in tpl:
        print(f"Template token {TOKEN!r} not found in template. Aborting.")
        sys.exit(3)

    films_json = serialize_films(films)

    # Replace the token with the JSON (note: token sits inside JS where a JS value is expected)
    final = tpl.replace(TOKEN, films_json)

    Path(out_path).write_text(final, encoding="utf-8")
    print(f"Wrote {out_path} ({len(films)} films embedded)")


def get_imdb_data(id, original_title, year) -> ImdbData | None:
    if not year:
        return None

    # short circuit with curations
    curations = load_curations()

    if id in curations:
        curation = curations[id]
        imdb_id = f"tt{curation.imdb_id.replace('tt', '')}"
        imdb_url = f"https://www.imdb.com/title/{imdb_id}/"
        if curation.imdb_score is not None:
            return ImdbData(imdb_url, curation.imdb_score)

        movie = web.get_title(imdb_id)
        imdb_rating = float(movie.rating) if movie.rating is not None else None
        return ImdbData(imdb_url, imdb_rating)

    results = IA.search_movie(original_title)
    for result in results:
        movie = web.get_title(f"tt{result.movieID}")
        if movie.year == int(year):
            movie = web.get_title(f"tt{result.movieID}")
            imdb_url = f"https://www.imdb.com/title/tt{result.movieID}/"
            imdb_rating = float(movie.rating) if movie.rating is not None else None
            return ImdbData(imdb_url=imdb_url, imdb_rating=imdb_rating)
    return None


def parse_event_links(html_content):
    soup = BeautifulSoup(html_content, "html.parser")
    event_links = []
    # get all divs with vc_grid-item class
    for div in soup.find_all("div", class_="vc_grid-item"):
        # get the first a tag
        a_tag = div.find("a")
        event_links.append(a_tag["href"])
    return event_links


def parse_datetime(date_str: str) -> datetime:
    # date_str example: "събота\n1 ноември 2025 г., 19:00 часа
    date_str = date_str.replace("\n", " ").strip()
    parsed = dateparser.parse(date_str, languages=["bg"])
    return parsed


def parse_film_details(url: str, html_content) -> Film:
    soup = BeautifulSoup(html_content, "html.parser")
    # h1 is title, h4 is original title
    title = soup.find("h1").text.strip()
    original_title = soup.find("h4").text.strip()
    # get div with film_shortinfo
    short_info = soup.find("div", class_="film_shortinfo")

    # find first p tag for year
    year = short_info.find("p").text.strip()
    year = "".join(filter(str.isdigit, year))[:4]  # Extract first 4 digits as year
    try:
        year = str(int(year))
    except ValueError:
        year = "N/A"

    paragraphs = short_info.find_all("p")
    genres = "N/A"
    language = "N/A"
    for p in paragraphs:
        if "Жанр:" in p.text:
            genres = p.text.replace("Жанр:", "").strip()
        if "Език:" in p.text:
            language = p.text.replace("Език:", "").strip()

    # now we find all screenings
    grids = soup.select(".vc_grid-gutter-30px")

    # Pick the first grid (or filter if there are multiple)
    grid = grids[0] if grids else soup

    screening = []
    for card in grid.select(".vc_grid-item"):
        # cinema title
        cinema_el = card.select_one(".cinema_title")
        cinema = cinema_el.get_text(strip=True) if cinema_el else None

        # the second vc_gitem-acf is datetime
        datetime_el = card.select_one(".vc_gitem-acf:nth-of-type(2)")
        datetime = datetime_el.get_text(" ", strip=True) if datetime_el else None

        # fallback: search for something that looks like time
        if not datetime:
            txt = card.get_text(" ", strip=True)
            match = re.search(r"\d{1,2}:\d{2}", txt)
            datetime = match.group(0) if match else None

        screening.append(Screening(datetime=parse_datetime(datetime), location=cinema))
    # sort the screenings by datetime
    screening.sort(key=lambda s: s.datetime)

    if original_title == "За филма":
        original_title = title
    year = int(year)
    if year < 1900 or year > 2100:
        year = None
    id = urlparse(url).path.strip("/")
    try:
        imdb = get_imdb_data(id, original_title, year)
    except Exception as e:
        imdb = None

    return Film(
        id=id,
        title=title,
        original_title=original_title,
        year=year,
        genres=genres,
        language=language,
        url=url,
        screenings=screening,
        imdb=imdb,
    )


async def get_film(url: str) -> Film:
    response = await CLIENT.get(url)
    response.raise_for_status()
    return parse_film_details(url, response.text)


async def main():
    response = await CLIENT.get(BASE_URL)
    response.raise_for_status()
    links = parse_event_links(response.text)
    films = []
    for link in links:
        films.append(await get_film(link))

    generate_index(films)


if __name__ == "__main__":
    asyncio.run(main())
