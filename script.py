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

# ---------------------------------------------------------------------------
# Model routing
# ---------------------------------------------------------------------------
# Single chat model (fast)
BASE_MODEL = "o4-mini-2025-04-16"

OPENAI_MODEL = os.getenv("OPENAI_MODEL", BASE_MODEL)
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
DEFAULT_TEST_MODE: bool = True  # enable test mode by default
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
        model_override: Optional[str] = None,
    ) -> Tuple[str, List[Dict[str, str]], List[Dict[str, str]], List[Dict[str, str]]]:

        model_id = model_override or OPENAI_MODEL
        async with self.semaphore:
            response = await self.client.chat.completions.create(
                model=model_id,
                messages=[
                    {"role": "system", "content": system_message},
                    {"role": "user",   "content": user_prompt},
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
    p.add_argument("--concurrency", type=int, default=6)
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
    with open(os.path.join(PROMPTS_DIR, "ideas_prompt.txt"), "r", encoding="utf-8") as fp:
        ideas_tpl = Template(fp.read())

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

        # Landing content -> always generated with BASE_MODEL
        content, faq, blogs, uses = await generator._single_call(SYSTEM_MESSAGE, prompt, model_override=BASE_MODEL)  # type: ignore

        # ------- second pass for enriched ideas (3 each) ---------------------
        related_q = [q.get("snippet") or q.get("question") for q in serp_context.get("related_questions", [])][:5]
        organic_snips = [o.get("snippet", "") for o in serp_context.get("organic_results", [])][:4]

        ideas_prompt = ideas_tpl.substitute(
            RELATED_Q="\n".join(f"- {q}" for q in related_q if q),
            ORGANIC_SNIPPETS="\n".join(f"- {s}" for s in organic_snips if s)
        )

        try:
            # We only need the blog and use-case lists (3 items each)
            _, _, extra_blogs, extra_uses = await generator._single_call(
                SYSTEM_MESSAGE,
                ideas_prompt,
                model_override=BASE_MODEL,  # single model
            )
            # merge while avoiding duplicates
            def _merge(src, extra, key):
                titles = {item[key] for item in src}
                for itm in extra:
                    if itm[key] not in titles and len(src) < 10:
                        src.append(itm)
            _merge(blogs, extra_blogs, "title")
            _merge(uses, extra_uses, "name")
        except Exception as e:
            logging.warning("Ideas prompt failed: %s", e)
        # Debug: log blog ideas and use cases returned by the model
        logging.debug("Row %d returned blog_ideas (%d): %s", idx, len(blogs), blogs)
        logging.debug("Row %d returned use_cases (%d): %s", idx, len(uses), uses)
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

    async def _write_page(
        path: str,
        title: str,
        body_html: str,
        extra_nav: str = "",
        meta_desc: str = "",
        model_id: str = "",
    ):
        """Write a fully-formed static HTML page."""

        page_slug = os.path.basename(path)
        canonical_url = (
            f"https://monsiaz.github.io/demo_happyscribe_contents_gen/{page_slug}"
        )

        skel = f"""
<!DOCTYPE html>
<html lang=\"en\">
<head>
    <meta charset=\"utf-8\">
    <title>{title} - HappyScribe</title>
    <meta name=\"description\" content=\"{meta_desc or 'AI-generated article about subtitle conversion.'}\">
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
    <link rel=\"canonical\" href=\"{canonical_url}\">

    <!-- Stylesheets -->
    <link rel=\"stylesheet\" href=\"https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css\">
    <link rel=\"stylesheet\" href=\"settings/web_assets/style.css\">
</head>
<body>
  <header class='topbar'>
    <div class='container topbar-inner'>
      <a class='brand' href='/'><img src='settings/web_assets/png-transparent-happy-scribe-logo-tech-companies.png' alt='HappyScribe logo'></a>
    </div>
  </header>

  <main class='container'>
     <nav id='toc' class='toc'></nav>
     {body_html}
     {extra_nav}
  </main>

  <footer>
    <div class='container small text-muted'>
       © HappyScribe · Generated by <strong>{model_id}</strong>
    </div>
  </footer>

  <script src=\"https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js\"></script>
  <script>
  document.addEventListener('DOMContentLoaded', () => {{
      const toc = document.getElementById('toc');
      if (!toc) return;
      const h2s = Array.from(document.querySelectorAll('main.container h2'));
      if (!h2s.length) {{ toc.remove(); return; }}
      const ul = document.createElement('ul');
      h2s.forEach(h => {{
          const id = h.textContent.trim().toLowerCase().replace(/[^a-z0-9]+/g, '-');
          h.id = id;
          const li = document.createElement('li');
          li.innerHTML = "<a href='#" + id + "'>" + h.textContent + "</a>";
          ul.appendChild(li);
      }});
      toc.innerHTML = '<h2>Table of contents</h2>';
      toc.appendChild(ul);
  }});
  </script>
</body>
</html>
"""

        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fp:
            fp.write(skel.strip())

    # ------------------------------------------------------------------
    # Helper: ensure we extract clean HTML from model response -----------
    _html_re = re.compile(r'"content"\\s*:\\s*"(.*?)"', re.DOTALL)

    def _ensure_html(raw: str) -> str:
        """Return HTML string even if wrapped in JSON or markdown fences."""
        txt = raw.strip()
        # Remove leading Response dumps
        if txt.startswith("Response("):
            first_tag = txt.find("<")
            if first_tag != -1:
                txt = txt[first_tag:]
        # If already HTML with headings
        if txt.startswith('<') and '<h' in txt:
            return txt
        # Remove ``` fences
        if txt.startswith('```'):
            txt = txt.lstrip('`').split('```', 1)[-1]
        m = _html_re.search(txt)
        if m:
            html_escaped = m.group(1)
            try:
                html_escaped = bytes(html_escaped, "utf-8").decode("unicode_escape")
            except Exception:
                pass
            return html_escaped
        return raw

    # CTA stripping helper removed – prompts no longer generate marketing calls

    def _build_faq_accordion(faq_list: List[Dict[str, str]], prefix: str) -> str:
        if not faq_list:
            return ""
        
        items_html = []
        for i, item in enumerate(faq_list):
            question = item.get("question", "")
            answer = item.get("answer", "")
            if not question or not answer:
                continue

            target_id = f"{prefix}-{i}"
            item_html = (
                f'<div class="accordion-item">'
                f'  <h3 class="accordion-header" id="heading-{target_id}">' 
                f'    <button class="accordion-button" type="button" data-bs-toggle="collapse" data-bs-target="#{target_id}" aria-expanded="true" aria-controls="{target_id}">' 
                f'      {question}' 
                f'    </button>' 
                f'  </h3>' 
                f'  <div id="{target_id}" class="accordion-collapse collapse show" aria-labelledby="heading-{target_id}" >' 
                f'    <div class="accordion-body">{answer}</div>' 
                f'  </div>' 
                f'</div>'
            )
            items_html.append(item_html)

        if not items_html:
            return ""

        return (
            f'<div class="faq-block"><h2>Frequently Asked Questions</h2>'
            f'<div class="accordion" id="faqAccordion-{prefix}">' 
            + ''.join(items_html) + '</div></div>'
        )

    # Convenience: slugify helper
    def _slugify(txt: str) -> str:
        base = unicodedata.normalize('NFKD', txt).encode('ascii', 'ignore').decode('ascii')
        slug = re.sub(r'[^a-z0-9]+', '-', base.lower()).strip('-')
        return slug[:60] or 'page'

    # ------------------------------------------------------------------
    # Generate blog, use-case, and landing pages for each result --------
    # ------------------------------------------------------------------

    for res in results:
        kw_p = res["keyword_primary"]
        kw_s = res["keyword_secondary"]
        f1 = res["format_1"]
        f2 = res["format_2"]

        slug = _slugify(f"{f1}-to-{f2}")
        landing_fname = f"{slug}.html"
        landing_path = os.path.join(demo_dir, landing_fname)

        # Build filename lists for blogs and use-cases
        blog_links = [
            (f"{slug}_blog_{i}.html", b.get("title", f"Blog {i}"), b.get("meta", ""))
            for i, b in enumerate(res.get("blog_ideas", []), 1)
        ]
        
        use_links = [
            (f"{slug}_use_{i}.html", u.get("name", f"Use case {i}"), u.get("description", ""))
            for i, u in enumerate(res.get("use_cases", []), 1)
        ]

        # Lists for context to avoid redundancy
        all_blog_titles = [title for _, title, _ in blog_links]
        all_use_names   = [name for _, name, _ in use_links]

        async def _gen_blog(fname: str, title_b: str, meta_b: str, idx: int):
            prompt = article_tpl.substitute(
                TITLE=title_b,
                KW_PRIMARY=kw_p,
                KW_SECONDARY=kw_s,
                ALL_BLOGS="\n".join(all_blog_titles),
                ALL_USES="\n".join(all_use_names),)
            model_to_use = BASE_MODEL
            body_html, *_ = await generator._single_call(
                SYSTEM_MESSAGE, prompt, model_override=model_to_use
            )
            nav_back = f'<p><a href="{landing_fname}">&larr; Back to converter landing</a></p>'
            extra_links = "<h2>More Articles</h2><ul>" + "".join(
                f'<li><a href="{f}">{t}</a></li>' for f, t, _ in blog_links if f != fname
            ) + "</ul>"
            await _write_page(
                os.path.join(demo_dir, fname),
                title_b,
                body_html,
                nav_back + extra_links,
                meta_b,
                model_id=model_to_use,
            )

        async def _gen_use(fname: str, name_u: str, desc_u: str, idx: int):
            prompt = use_tpl.substitute(
                USE_NAME=name_u,
                F1=f1.upper(),
                F2=f2.upper(),
                ALL_BLOGS="\n".join(all_blog_titles),
                ALL_USES="\n".join(all_use_names),)
            model_to_use = BASE_MODEL
            body_html, *_ = await generator._single_call(
                SYSTEM_MESSAGE, prompt, model_override=model_to_use
            )
            nav_back = f'<p><a href="{landing_fname}">&larr; Back to converter landing</a></p>'
            extra_links = "<h2>More Use Cases</h2><ul>" + "".join(
                f'<li><a href="{f}">{n}</a></li>' for f, n, _ in use_links if f != fname
            ) + "</ul>"
            await _write_page(
                os.path.join(demo_dir, fname),
                name_u,
                body_html,
                nav_back + extra_links,
                desc_u,
                model_id=model_to_use,
            )

        tasks: List[asyncio.Task] = []
        tasks += [
            asyncio.create_task(_gen_blog(f, t, m, idx))
            for idx, (f, t, m) in enumerate(blog_links, 1)
        ]
        tasks += [
            asyncio.create_task(_gen_use(f, n, d, idx))
            for idx, (f, n, d) in enumerate(use_links, 1)
        ]
        await asyncio.gather(*tasks)

        # ---------------- Landing page ----------------
        content_html = _ensure_html(res["content"])
        comp_html = _ensure_html(res.get("comparison", ""))
        faq_html = _build_faq_accordion(res.get("faq", []), slug)

        nav_html = ""
        block_parts = []
        if blog_links:
            block_parts.append(
                "<h2>Related Articles</h2><ul class='related-list'>" + "".join(
                    f'<li><a href=\"{fname}\">{title}</a></li>' for fname, title, _ in blog_links
                ) + "</ul>"
            )
        if use_links:
            block_parts.append(
                "<h2>Professional Use Cases</h2><ul class='related-list'>" + "".join(
                    f'<li><a href=\"{fname}\">{name}</a></li>' for fname, name, _ in use_links
                ) + "</ul>"
            )
        if block_parts:
            nav_html = "<hr class='section-divider'><div class='block-section'>" + "<hr class='section-divider'>".join(block_parts) + "</div>"

        await _write_page(
            landing_path,
            kw_p,
            content_html + comp_html + faq_html + nav_html,
            model_id=BASE_MODEL,
        )

        logging.info(
            "✅ Generated landing + %d blogs + %d use cases for %s → %s",
            len(blog_links),
            len(use_links),
            f1.upper(),
            f2.upper(),
        )

    # ------------------------------------------------------------------
    # Generate an index.html listing all generated pages
    pages = sorted([f for f in os.listdir(demo_dir) if f.endswith('.html')])
    list_items = ''.join(f'<li><a href="{p}">{p.replace(".html", " ").replace("-", " ").title()}</a></li>' for p in pages)
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