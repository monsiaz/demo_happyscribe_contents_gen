import os
import argparse
import asyncio
import json
import logging
import re
import unicodedata
from typing import Tuple, Optional, List, Dict

import pandas as pd
from serpapi import GoogleSearch
from string import Template

# ---------------------------------------------------------------------------
# Secrets loader                                                             
# ---------------------------------------------------------------------------

def _load_keys() -> Dict[str, str]:
    """Load OPENAI_API_KEY and SERPAPI_API_KEY from settings/keys.txt if present."""
    key_file = os.path.join(os.path.dirname(__file__), "settings", "keys.txt")
    if not os.path.isfile(key_file):
        return {}
    secrets: Dict[str, str] = {}
    with open(key_file, "r", encoding="utf-8") as fp:
        for line in fp:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            secrets[k.strip()] = v.strip()
    return secrets

_SECRETS = _load_keys()

###############################################################################
# Configuration helpers                                                       #
###############################################################################

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "o4-mini-2025-04-16")
OPENAI_API_KEY = _SECRETS.get("OPENAI_API_KEY")
SERPAPI_API_KEY = _SECRETS.get("SERPAPI_API_KEY", "")

SYSTEM_MESSAGE = (
    "You are an expert copywriter specialized in crafting structured, professional, "
    "and helpful landing-page content for audiences interested in converting "
    "subtitle and transcript files between formats."
)

# Fallback placeholder – must be overridden via OPENAI_API_KEY environment variable or settings/keys.txt
HARDCODED_API_KEY = ""  # DO NOT store real keys in the repo

# ---------------------------------------------------------------------------
# User-tweakable defaults                                                     
# ---------------------------------------------------------------------------
DEFAULT_TEST_MODE: bool = False  # disable test mode by default
TEST_ROW_COUNT: int = 1  # rows processed when test mode active

###############################################################################
# Prompt builder                                                              #
###############################################################################

PROMPTS_DIR = os.path.join(os.path.dirname(__file__), "Prompts")


def build_outline_prompt(
    format_1: str,
    format_2: str,
    kw_primary: str,
    kw_secondary: str,
    related_searches: List[str],
) -> str:
    """Build landing-page prompt using external template."""

    # Example block (shown to model)
    example_section = """
Example for the keyword "convert vtt to srt" (only to illustrate the expected structure – do not reuse wording):
{
  "content": "<h1>Convert vtt to srt: quick guide</h1> ... (html body omitted)",
  "faq": [
    {"question": "Can I convert VTT to SRT online?", "answer": "Yes ..."},
    {"question": "Is the timing preserved?", "answer": "Absolutely ..."}
  ],
  "blog_ideas": [
    {"title": "How subtitles improve your video seo: the critical role of SRT files", "meta": "An analysis of how adding .srt subtitles boosts discoverability."}
  ],
  "use_cases": [
    {"name": "Film and TV post-production", "description": "SRT used to subtitle movies ..."}
  ]
}
"""

    # Related searches list
    related_prompt = ""
    if related_searches:
        related_prompt = "\n".join([f'- "{s}"' for s in related_searches])

    # Cache template
    if not hasattr(build_outline_prompt, "_tpl"):
        with open(os.path.join(PROMPTS_DIR, "landing_prompt.txt"), "r", encoding="utf-8") as fp:
            build_outline_prompt._tpl = Template(fp.read())  # type: ignore
    tpl: Template = build_outline_prompt._tpl  # type: ignore

    return tpl.substitute(
        F1=format_1.upper(),
        F2=format_2.upper(),
        KW_PRIMARY=kw_primary,
        KW_SECONDARY=kw_secondary,
        RELATED_LIST=related_prompt,
        EXAMPLE=example_section,
    )

###############################################################################
# OpenAI wrapper                                                              #
###############################################################################

try:
    from openai import AsyncOpenAI  # type: ignore
except ImportError as e:
    raise SystemExit("openai package not found. Install with `pip install openai>=1.3.7`.") from e


class SEOGenerator:
    """Utility class for asynchronous OpenAI calls with a semaphore."""

    def __init__(self, api_key: Optional[str] = None, serpapi_api_key: Optional[str] = None, concurrency: int = 3):
        if api_key is None:
            api_key = OPENAI_API_KEY or HARDCODED_API_KEY
        self.client = AsyncOpenAI(api_key=api_key)
        self.serpapi_api_key = serpapi_api_key
        self.semaphore = asyncio.Semaphore(concurrency)

    async def _single_call(
        self,
        system_message: str,
        user_prompt: str,
    ) -> Tuple[str, List[Dict[str, str]], List[Dict[str, str]], List[Dict[str, str]]]:
        """Call the chat model and return parsed JSON sections."""
        async with self.semaphore:
            response = await self.client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=[
                    {"role": "system", "content": system_message},
                    {"role": "user", "content": user_prompt},
                ],
            )
            answer = response.choices[0].message.content

        # Remove ```json fences if present
        import re, textwrap

        def _strip(txt: str) -> str:
            m = re.search(r"```(?:json)?\s*(\{.*?\})```", txt, re.DOTALL)
            return textwrap.dedent(m.group(1)).strip() if m else txt.strip()

        cleaned = _strip(answer)
        parsed = None
        # Try parsing JSON; fallback to Python literal eval if it fails
        try:
            parsed = json.loads(cleaned)
        except Exception:
            try:
                import ast
                parsed = ast.literal_eval(cleaned)
            except Exception:
                parsed = None

        if parsed:
            return (
                parsed.get("content", ""),
                parsed.get("faq", []),
                parsed.get("blog_ideas", []),
                parsed.get("use_cases", []),
            )
        # fallback
        return cleaned, [], [], []

###############################################################################
# Helper: related searches                                                    #
###############################################################################

def gather_serp_context(query: str, api_key: str) -> Dict[str, object]:
    """Return SERP context: suggestions list plus raw blocks (questions, overview, organic)."""
    if not api_key:
        return {"suggestions": []}
    try:
        params = {"engine": "google_light", "q": query, "api_key": api_key}
        results = GoogleSearch(params).get_dict()

        suggestions: List[str] = []
        questions_raw: List[dict] = results.get("related_questions", [])
        organic_raw: List[dict] = results.get("organic_results", [])

        # related searches (queries)
        for item in results.get("related_searches", []):
            q = item.get("query")
            if q:
                suggestions.append(q)

        # related questions
        for item in questions_raw:
            q = item.get("question") or item.get("title")
            if q and q not in suggestions:
                suggestions.append(q)

        # AI overview summary paragraphs (if available)
        ai_overview = results.get("ai_overview")
        if isinstance(ai_overview, dict):
            summary = ai_overview.get("summary") or ai_overview.get("answer")
            if summary:
                suggestions.extend([s.strip() for s in summary.split(". ") if s])
        elif isinstance(ai_overview, str):
            suggestions.extend([s.strip() for s in ai_overview.split(". ") if s])

        return {
            "suggestions": suggestions[:20],
            "related_questions": questions_raw,
            "ai_overview": ai_overview,
            "organic_results": organic_raw,
        }
    except Exception as e:
        logging.warning("SerpApi error: %s", e)
        return {"suggestions": []}

###############################################################################
# CLI                                                                         #
###############################################################################

DEFAULT_CSV = os.path.join("settings", "srt_contents.csv")
DEFAULT_OUTPUT = "preview.json"

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Generate SEO landing content + pages.")
    p.add_argument("--input_csv", default=DEFAULT_CSV)
    p.add_argument("--output_json", default=DEFAULT_OUTPUT)
    p.add_argument("--concurrency", type=int, default=3)
    p.add_argument("--test", action="store_true", default=DEFAULT_TEST_MODE, help="enable test mode to process only top rows")
    p.add_argument("--limit", type=int)
    p.add_argument("--debug", action="store_true")
    return p.parse_args()

###############################################################################
# Main async workflow                                                         #
###############################################################################

async def main() -> None:
    args = parse_args()
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

    df = pd.read_csv(args.input_csv)

    # test mode
    if args.test:
        sv_col = next((c for c in df.columns if c.startswith("SV")), None)
        rows_keep = TEST_ROW_COUNT if TEST_ROW_COUNT > 0 else 5
        if sv_col:
            df[sv_col] = pd.to_numeric(df[sv_col], errors="coerce").fillna(0)
            df = df.sort_values(sv_col, ascending=False).head(rows_keep).copy()
        else:
            df = df.head(rows_keep).copy()
    if args.limit:
        df = df.head(args.limit).copy()

    generator = SEOGenerator(concurrency=args.concurrency, serpapi_api_key=SERPAPI_API_KEY)

    with open(os.path.join(PROMPTS_DIR, "usecase_prompt.txt"), "r", encoding="utf-8") as fp:
        use_tpl = Template(fp.read())
    with open(os.path.join(PROMPTS_DIR, "comparison_prompt.txt"), "r", encoding="utf-8") as fp:
        comp_tpl = Template(fp.read())

    async def _wrap(idx: int, row: pd.Series):
        kw_primary = row["Keyword 1"]
        kw_secondary = row["Keyword 2"]
        serp_context: Dict[str, object] = {"suggestions": []}
        if SERPAPI_API_KEY:
            serp_context = await asyncio.to_thread(gather_serp_context, kw_primary, SERPAPI_API_KEY)
        related_searches = serp_context.get("suggestions", [])

        prompt = build_outline_prompt(
            format_1=row["format_1"],
            format_2=row["format_2"],
            kw_primary=kw_primary,
            kw_secondary=kw_secondary,
            related_searches=related_searches,
        )

        content, faq, blogs, uses = await generator._single_call(SYSTEM_MESSAGE, prompt)  # type: ignore
        comp_prompt = comp_tpl.substitute(FORMAT_1=row["format_1"].upper(), FORMAT_2=row["format_2"].upper())
        comp_html, *_ = await generator._single_call(SYSTEM_MESSAGE, comp_prompt)  # type: ignore
        return idx, content, faq, blogs, uses, serp_context, comp_html

    tasks = [asyncio.create_task(_wrap(idx, row)) for idx, row in df.iterrows()]
    logging.info("Queued %d rows", len(tasks))

    results: List[Dict[str, object]] = []
    for fut in asyncio.as_completed(tasks):
        idx, content, faq, blogs, uses, rel, comp_html = await fut
        results.append({
            "url": df.at[idx, "URL"],
            "format_1": df.at[idx, "format_1"],
            "format_2": df.at[idx, "format_2"],
            "keyword_primary": df.at[idx, "Keyword 1"],
            "keyword_secondary": df.at[idx, "Keyword 2"],
            "serp_context": rel,
            "content": content,
            "faq": faq,
            "blog_ideas": blogs,
            "use_cases": uses,
            "comparison": comp_html,
        })
        with open(args.output_json, "w", encoding="utf-8") as fp:
            json.dump(results, fp, ensure_ascii=False, indent=2)

    # ------------------------------------------------------------------
    # Generate static pages                                              
    # ------------------------------------------------------------------

    demo_dir = os.path.join(os.path.dirname(__file__), "demo_site")
    os.makedirs(demo_dir, exist_ok=True)

    with open(os.path.join(PROMPTS_DIR, "article_prompt.txt"), "r", encoding="utf-8") as fp:
        article_tpl = Template(fp.read())

    async def _write_page(path: str, title: str, body_html: str, extra_nav: str = "", meta_desc: str = ""):
        """Write an HTML page with optional navigation/footer."""
        skel = (
            "<!DOCTYPE html><html><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{title}</title>"
            "<link href='https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css' rel='stylesheet'>"
            "<link rel='stylesheet' href='settings/web_assets/style.css'>"
            "</head><body>"
            "<header class='topbar'><div class='container topbar-inner'>"
            "<a class='brand' href='https://www.happyscribe.com'><img src='settings/web_assets/logo.png' alt='HappyScribe logo'></a>"
            "<nav class='main-nav'>"
            "<a href='#'>Tools</a>"
            "<a href='#'>Resources</a>"
            "<a href='#'>Pricing</a>"
            "<a href='#' class='btn-primary'>Start for Free</a>"
            "</nav></div></header>"
            "<main class='container'>"
            f"{body_html}"
            f"{extra_nav}"
            "</main>"
            "<footer><div class='container'><p>&copy; HappyScribe</p></div></footer>"
            "</body></html>"
        )
        with open(path, "w", encoding="utf-8") as fp:
            fp.write(skel)

    # Utility: extract pure HTML from a raw JSON-ish string returned by the model
    _html_re = re.compile(r'"content"\s*:\s*"(.*?)"', re.DOTALL)
    def _ensure_html(raw: str) -> str:
        """Return HTML string: if `raw` is full JSON markdown block, pull the content field."""
        txt = raw.strip()
        # If it already looks like HTML (contains <h1> and not '{'), return as-is
        if txt.startswith('<') and '<h' in txt:
            return txt
        # Try to remove ``` fences
        if txt.startswith('```'):
            txt = txt.lstrip('`').split('```', 1)[-1]
        m = _html_re.search(txt)
        if m:
            html_escaped = m.group(1)
            try:
                # Unescape typical \n, \t, etc.
                html_escaped = bytes(html_escaped, "utf-8").decode("unicode_escape")
            except Exception:
                pass
            return html_escaped
        return raw  # fallback

    # Convenience: slugify helper
    def _slugify(txt: str) -> str:
        base = unicodedata.normalize('NFKD', txt).encode('ascii', 'ignore').decode('ascii')
        slug = re.sub(r'[^a-z0-9]+', '-', base.lower()).strip('-')
        return slug[:60] or 'page'

    for row in results:
        kw_p = row["keyword_primary"]
        kw_s = row["keyword_secondary"]
        f1 = row["format_1"]
        f2 = row["format_2"]
        # Prepare slug and paths
        slug = re.sub(r"[^a-z0-9]+", "-", kw_p.lower()).strip("-")
        landing_path = os.path.join(demo_dir, f"{slug}.html")

        # --- Generate BLOG pages -------------------------------------------------
        blog_links: List[Tuple[str, str, str]] = []  # (fname, title, meta)
        for idx, blog in enumerate(row["blog_ideas"], 1):
            fname = f"blog_{idx}_{_slugify(blog['title'])}.html"
            blog_links.append((fname, blog["title"], blog.get("meta", "")))

        for fname, title_b, meta_b in blog_links:
            dst = os.path.join(demo_dir, fname)
            if not os.path.exists(dst):
                prompt = article_tpl.substitute(TITLE=title_b, KW_PRIMARY=kw_p, KW_SECONDARY=kw_s)
                body_html, *_ = await generator._single_call(SYSTEM_MESSAGE, prompt)  # type: ignore
                nav_back = f'<p><a href="{slug}.html">&larr; Back to converter landing</a></p>'
                extra_links = "<h2>More Articles</h2><ul>" + "".join(
                    f'<li><a href="{f}">{t}</a></li>' for f, t, _ in blog_links if f != fname
                ) + "</ul>"
                await _write_page(dst, title_b, body_html, nav_back + extra_links, meta_b)

        # --- Generate USE-CASE pages -------------------------------------------
        use_links: List[Tuple[str, str, str]] = []  # (fname, name, desc)
        for idx, use in enumerate(row["use_cases"], 1):
            fname = f"use_{idx}_{_slugify(use['name'])}.html"
            use_links.append((fname, use["name"], use.get("description", "")))

        for fname, name_u, desc_u in use_links:
            dst = os.path.join(demo_dir, fname)
            if not os.path.exists(dst):
                prompt = use_tpl.substitute(USE_NAME=name_u, F1=f1.upper(), F2=f2.upper())
                body_html, *_ = await generator._single_call(SYSTEM_MESSAGE, prompt)  # type: ignore
                nav_back = f'<p><a href="{slug}.html">&larr; Back to converter landing</a></p>'
                extra_links = "<h2>More Use Cases</h2><ul>" + "".join(
                    f'<li><a href="{f}">{n}</a></li>' for f, n, _ in use_links if f != fname
                ) + "</ul>"
                await _write_page(dst, name_u, body_html, nav_back + extra_links, desc_u)

        # --- Write (or rewrite) the landing page with navigation ---------------
        content_html = _ensure_html(row["content"])
        comp_html = _ensure_html(row.get("comparison", ""))

        if content_html:
            nav_html = ""
            if blog_links:
                nav_html += "<h2>Related Articles</h2><ul>" + "".join(
                    f'<li><a href="{fname}">{title}</a></li>' for fname, title, _ in blog_links
                ) + "</ul>"
            if use_links:
                nav_html += "<h2>Professional Use Cases</h2><ul>" + "".join(
                    f'<li><a href="{fname}">{name}</a></li>' for fname, name, _ in use_links
                ) + "</ul>"

            landing_html = content_html + comp_html + nav_html
            # Always overwrite to ensure links are up to date
            await _write_page(landing_path, kw_p, landing_html)

    # Generate an index.html listing all generated pages
    pages = sorted([f for f in os.listdir(demo_dir) if f.endswith('.html')])
    list_items = ''.join(f'<li><a href="{p}">{p.replace(".html"," ").replace('-',' ').title()}</a></li>' for p in pages)
    index_content = (
        '<!DOCTYPE html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>Demo Index</title>'
        '<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">'
        '<link rel="stylesheet" href="settings/web_assets/style.css">'
        '</head><body><div class="container">'
        '<h1>Available Pages</h1><ul>' + list_items + '</ul></div></body></html>'
    )
    with open(os.path.join(demo_dir, 'index.html'), 'w', encoding='utf-8') as f:
        f.write(index_content)
    logging.info("Static pages generated in %s", demo_dir)

if __name__ == "__main__":
    asyncio.run(main()) 