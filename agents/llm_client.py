"""
llm_client.py — Provider-agnostic LLM wrapper for Your Humble EquityBot.

Supports Claude (Anthropic) and GPT-4o (OpenAI) with identical interface.
Switch providers by changing LLM_PROVIDER in .env — no other code changes needed.

Adversarial mode: run both providers and cross-review each other's analysis.
"""

from __future__ import annotations
import json
import logging
import re
import time
from typing import Optional

from config import (
    ANTHROPIC_API_KEY, OPENAI_API_KEY, DEEPSEEK_API_KEY, MOONSHOT_API_KEY, GEMINI_API_KEY,
    LLM_PROVIDER, LLM_MODEL, ADVERSARIAL_MODE,
    KIMI_MAX_TOKENS_MULTIPLIER, KIMI_MAX_TOKENS_CAP, KIMI_REQUEST_TIMEOUT_SECONDS,
    GEMINI_MAX_TOKENS_MULTIPLIER, GEMINI_MAX_TOKENS_CAP, GEMINI_REQUEST_TIMEOUT_SECONDS,
    GEMINI_REASONING_EFFORT,
)

logger = logging.getLogger(__name__)


def _eq_b(v) -> str:
    if v is None:
        return "n/a"
    return f"{v/1000:.1f}B" if abs(v) >= 1000 else f"{v:.1f}M"


def _eq_pct(v) -> str:
    return f"{v*100:+.1f}%" if v is not None else "n/a"


def _eq_pct_plain(v) -> str:
    return f"{v*100:.1f}%" if v is not None else "n/a"


def _format_earnings_grounding(company) -> str:
    """
    Build a verified-figures block from the company's own fetched financial
    data (latest reported fiscal year + YoY deltas + forward consensus),
    so the news-narrative prompt has accurate revenue/profit numbers to
    anchor on instead of relying solely on the model's own knowledge or
    on NewsAPI headlines that rarely carry the actual reported figures.

    Returns "" if no usable annual data is available (e.g. thin-data ticker).
    """
    if company is None:
        return ""
    try:
        years = company.sorted_years()
    except Exception:
        return ""
    if not years:
        return ""

    cur = company.currency or ""
    latest = company.annual_financials.get(years[0])
    prior = company.annual_financials.get(years[1]) if len(years) > 1 else None
    if latest is None:
        return ""

    def _yoy(curr, prev):
        if curr is None or prev in (None, 0):
            return None
        return (curr - prev) / abs(prev)

    lines = [
        f"Most recently reported fiscal year: FY{latest.year} "
        f"(currency: {cur}).",
        f"  Revenue: {_eq_b(latest.revenue)} "
        f"(YoY: {_eq_pct(_yoy(latest.revenue, prior.revenue if prior else None))})",
        f"  Operating profit / EBIT: {_eq_b(latest.ebit)} "
        f"(YoY: {_eq_pct(_yoy(latest.ebit, prior.ebit if prior else None))})",
        f"  Net income: {_eq_b(latest.net_income)} "
        f"(YoY: {_eq_pct(_yoy(latest.net_income, prior.net_income if prior else None))})",
    ]
    if latest.ebit_margin is not None:
        lines.append(f"  Operating margin: {_eq_pct_plain(latest.ebit_margin)}")
    if latest.net_margin is not None:
        lines.append(f"  Net margin: {_eq_pct_plain(latest.net_margin)}")

    ttm_rev = getattr(company, "ttm_revenue", None)
    ttm_ni  = getattr(company, "ttm_net_income", None)
    ttm_date = getattr(company, "ttm_last_quarter_date", None)
    if ttm_rev is not None or ttm_ni is not None:
        lines.append(
            f"Trailing twelve months (through {ttm_date or 'most recent quarter'}): "
            f"Revenue {_eq_b(ttm_rev)}, Net income {_eq_b(ttm_ni)}."
        )

    fe = getattr(company, "forward_estimates", None)
    if fe is not None and (fe.revenue is not None or fe.eps_diluted is not None):
        lines.append(
            f"Analyst consensus forecast for FY{fe.year}: "
            f"Revenue {_eq_b(fe.revenue)}"
            + (f" ({_eq_pct(fe.revenue_growth_yoy)} YoY)" if fe.revenue_growth_yoy is not None else "")
            + (f", EPS {fe.eps_diluted:.2f}" if fe.eps_diluted is not None else "")
            + "."
        )

    next_ed = getattr(company, "next_earnings_date", None)
    if next_ed:
        lines.append(f"Next scheduled earnings date: {next_ed}.")

    return "\n".join(lines)


class LLMClient:
    """
    Single interface to any configured LLM provider.

    Usage:
        client = LLMClient()
        text   = client.generate(user_prompt, system_prompt)
        parsed = client.generate_json(user_prompt, system_prompt)

    Prompt caching (Claude only):
        Pass cacheable_prefix=<fixed_text> to generate()/generate_json().
        The prefix is sent as a separate content block marked cache_control:ephemeral.
        Anthropic caches it for 5 minutes — 90% cost reduction on re-reads.
        Requires ≥ 1024 tokens in the prefix + system prompt combined.

    Token usage:
        After each Claude call, self.last_usage is populated:
        {input_tokens, output_tokens, cache_creation_input_tokens, cache_read_input_tokens}
    """

    def __init__(self, provider: str = "", model: str = ""):
        self.provider   = provider or LLM_PROVIDER
        self.model      = model    or LLM_MODEL
        self.last_usage: dict = {}   # populated after each Claude call

        if not self._api_key():
            logger.warning(
                f"[LLMClient] No API key found for provider '{self.provider}'. "
                f"Add the key to your .env file."
            )

    # ── Public API ────────────────────────────────────────────────────────────

    def generate(
        self,
        user_prompt: str,
        system_prompt: str = "",
        max_tokens: int = 4096,
        temperature: float = 0.3,
        cacheable_prefix: str = "",
        force_json: bool = False,
    ) -> str:
        """
        Generate a text response from the configured LLM.
        Temperature 0.3 = creative but consistent (good for analyst reports).

        cacheable_prefix: fixed text sent before user_prompt as a separate content
        block with cache_control:ephemeral (Claude only). Use this for the framework
        instructions / output schema portion of the prompt — it stays the same across
        runs of the same framework, so Anthropic can cache and re-read it cheaply.

        force_json: when True, request structured JSON output from the provider
        (OpenAI: response_format=json_object). Used by generate_json().
        """
        start = time.time()
        logger.info(f"[LLMClient] Calling {self.provider}/{self.model} "
                    f"(~{(len(cacheable_prefix)+len(user_prompt))//4} tokens in)…")

        if self.provider == "claude":
            result = self._claude(user_prompt, system_prompt, max_tokens, temperature,
                                  cacheable_prefix=cacheable_prefix,
                                  force_json=force_json)
        elif self.provider == "openai":
            result = self._openai(user_prompt, system_prompt, max_tokens, temperature,
                                  cacheable_prefix=cacheable_prefix,
                                  force_json=force_json)
        elif self.provider == "deepseek":
            result = self._openai(user_prompt, system_prompt, max_tokens, temperature,
                                  cacheable_prefix=cacheable_prefix,
                                  force_json=force_json,
                                  base_url="https://api.deepseek.com",
                                  api_key=DEEPSEEK_API_KEY)
        elif self.provider == "kimi":
            # Every caller's requested max_tokens is a completion-budget floor
            # for the actual answer — scale it up here (not at each call site)
            # so every report type gets more headroom for kimi automatically.
            _kimi_max_tokens = min(int(max_tokens * KIMI_MAX_TOKENS_MULTIPLIER),
                                    KIMI_MAX_TOKENS_CAP)
            result = self._openai(user_prompt, system_prompt, _kimi_max_tokens, temperature,
                                  cacheable_prefix=cacheable_prefix,
                                  force_json=force_json,
                                  base_url="https://api.moonshot.ai/v1",
                                  api_key=MOONSHOT_API_KEY,
                                  request_timeout=KIMI_REQUEST_TIMEOUT_SECONDS)
        elif self.provider == "gemini":
            # Same rationale as the kimi branch above — Gemini 2.5 Pro also
            # spends hidden reasoning tokens before the final JSON answer.
            _gemini_max_tokens = min(int(max_tokens * GEMINI_MAX_TOKENS_MULTIPLIER),
                                     GEMINI_MAX_TOKENS_CAP)
            result = self._openai(user_prompt, system_prompt, _gemini_max_tokens, temperature,
                                  cacheable_prefix=cacheable_prefix,
                                  force_json=force_json,
                                  base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
                                  api_key=GEMINI_API_KEY,
                                  request_timeout=GEMINI_REQUEST_TIMEOUT_SECONDS,
                                  reasoning_effort=GEMINI_REASONING_EFFORT)
        else:
            raise ValueError(f"Unknown LLM provider: '{self.provider}'. "
                             f"Set LLM_PROVIDER=claude, openai, deepseek, kimi, or gemini in .env")

        elapsed = time.time() - start
        u = self.last_usage
        cache_hit = u.get("cache_read_input_tokens", 0)
        cache_new = u.get("cache_creation_input_tokens", 0)
        logger.info(
            f"[LLMClient] Response: ~{len(result)//4} tokens out, {elapsed:.1f}s"
            + (f" | cache_hit={cache_hit} cache_write={cache_new}" if (cache_hit or cache_new) else "")
        )
        return result

    def generate_json(
        self,
        user_prompt: str,
        system_prompt: str = "",
        max_tokens: int = 4096,
        cacheable_prefix: str = "",
    ) -> dict:
        """
        Generate and parse a JSON response.
        Automatically handles markdown code blocks and minor formatting issues.
        Falls back to empty dict on parse failure with an error log.

        cacheable_prefix: see generate() — passed through unchanged.

        Side effect: stores the raw response on `self.last_raw_response` so
        callers can show / log what the model actually said when parsing
        fails downstream.
        """
        # Ask explicitly for JSON output
        json_instruction = (
            "\n\nIMPORTANT: Return ONLY valid JSON. "
            "No markdown, no code blocks, no commentary before or after the JSON object."
        )
        raw = self.generate(user_prompt + json_instruction, system_prompt, max_tokens,
                            cacheable_prefix=cacheable_prefix,
                            force_json=True)
        self.last_raw_response = raw or ""

        # Strip any markdown code fences the model might add despite instructions
        cleaned = _strip_code_fences(raw)

        # strict=False allows literal control characters (unescaped newlines,
        # tabs) inside string values — GPT-4o frequently writes multi-paragraph
        # "snapshot" text with raw newlines instead of escaping them as \n,
        # which json.loads() otherwise rejects outright as a parse error even
        # though the JSON is structurally fine.
        try:
            return json.loads(cleaned, strict=False)
        except json.JSONDecodeError:
            # Fallback 1: find first { to last } (handles trailing commentary)
            start = cleaned.find('{')
            end   = cleaned.rfind('}')
            if start != -1 and end != -1 and end > start:
                try:
                    return json.loads(cleaned[start:end+1], strict=False)
                except json.JSONDecodeError:
                    pass
            # Fallback 2: regex extraction
            match = re.search(r'\{.*\}', cleaned, re.DOTALL)
            if match:
                try:
                    return json.loads(match.group(0), strict=False)
                except json.JSONDecodeError:
                    pass
            # Fallback 3: truncated-JSON repair (max_tokens hit mid-response)
            repaired = _try_repair_truncated_json(cleaned)
            if repaired:
                try:
                    salvaged = json.loads(repaired)
                    logger.warning(
                        f"[LLMClient] JSON was truncated — salvaged "
                        f"{len(salvaged)} top-level keys via repair."
                    )
                    return salvaged
                except json.JSONDecodeError:
                    pass
            logger.error(
                f"[LLMClient] JSON parse failed. Raw response (first 1000 chars):\n"
                f"{raw[:1000]}"
            )
            return {}

    def generate_web_news(self, company_name: str, ticker: str, company=None) -> str:
        """
        Search the web for recent news about the company and return a
        narrative analyst-style overview (plain markdown text).

        Uses provider-native web search:
          - OpenAI: Responses API with web_search_preview tool
          - Claude: messages API with web_search_20250305 tool

        `company` (CompanyData, optional): when supplied, the company's own
        latest reported fiscal-year figures (revenue, operating profit, net
        income, YoY deltas, forward consensus) are handed to the model as a
        verified-figures block, and the model is explicitly told to include
        a dedicated "Latest Earnings Report" theme built from them —
        web search alone often misses exact reported numbers or is limited
        by the model's training cutoff, so this guarantees the section is
        grounded in the actual data already fetched for the report.

        Returns plain text (markdown with **bold** section headers).
        Falls back to empty string on any error.
        """
        try:
            grounding = _format_earnings_grounding(company)
        except Exception as e:
            logger.warning(f"[LLMClient] Earnings grounding build failed, continuing without it: {e}")
            grounding = ""
        earnings_instruction = (
            'Always include one theme called "Latest Earnings Report" — cover the most '
            'recently reported fiscal year/quarter\'s revenue, operating profit, and net '
            'income, each with its year-over-year % change, plus forward guidance for the '
            'next fiscal year and the market/stock-price reaction to the results if you can '
            'find it via search. '
            + (
                f'Use these VERIFIED figures as the source of truth for revenue/profit/YoY '
                f'numbers (do not contradict them; you may add color from search on top of '
                f'them, e.g. market reaction, guidance detail, analyst commentary):\n{grounding}\n\n'
                if grounding else
                'No pre-verified figures were supplied — search for and cite the company\'s '
                'actual reported figures from its most recent earnings release. '
            )
        )
        prompt = (
            f'Search the web for recent news about {company_name} ({ticker}). '
            f'Write a senior equity analyst narrative overview — organised into 3-5 themes '
            f'(e.g. "Latest Earnings Report", "Strategic moves & M&A", "Insider activity"). '
            f'{earnings_instruction}'
            f'Format: each theme as **Theme name** on its own line, then 2-4 continuous '
            f'prose sentences — NO bullet points, NO numbered lists, NO citation brackets like [1]. '
            f'Include specific dates (e.g. "In March 2026...") and numbers inline in the prose. '
            f'Write each theme as ONE flowing paragraph, not fragmented lines. '
            f'Do NOT include a document title or heading at the top. '
            f'Plain text with **bold** theme headers only — no other markdown, no JSON.'
        )
        try:
            if self.provider == "openai":
                return self._openai_web_search(prompt)
            elif self.provider == "claude":
                return self._claude_web_search(prompt)
            elif self.provider == "deepseek":
                return self._deepseek_news_summary(company_name, ticker, grounding=grounding)
            elif self.provider == "kimi":
                return self._deepseek_news_summary(
                    company_name, ticker,
                    base_url="https://api.moonshot.ai/v1", api_key=MOONSHOT_API_KEY,
                    grounding=grounding,
                )
            elif self.provider == "gemini":
                return self._deepseek_news_summary(
                    company_name, ticker,
                    base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
                    api_key=GEMINI_API_KEY,
                    grounding=grounding,
                )
        except Exception as e:
            logger.warning(f"[LLMClient] Web news search failed: {e}")
        return ""

    def _openai_web_search(self, prompt: str) -> str:
        """Call OpenAI Responses API with web_search_preview tool."""
        try:
            from openai import OpenAI
        except ImportError:
            raise ImportError("Run: pip install openai")
        client = OpenAI(api_key=OPENAI_API_KEY)
        resp = client.responses.create(
            model=self.model,
            tools=[{"type": "web_search_preview"}],
            input=prompt,
        )
        return resp.output_text or ""

    def _claude_web_search(self, prompt: str) -> str:
        """Call Claude messages API with built-in web_search tool."""
        try:
            import anthropic
        except ImportError:
            raise ImportError("Run: pip install anthropic")
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        msg = client.messages.create(
            model=self.model,
            max_tokens=4096,
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            messages=[{"role": "user", "content": prompt}],
        )
        # Collect all text blocks from the response
        parts = [b.text for b in msg.content if hasattr(b, "text") and b.text]
        if not parts:
            logger.warning(
                f"[LLMClient] Claude web search returned no text blocks "
                f"(stop_reason={getattr(msg, 'stop_reason', '?')}, "
                f"block_types={[getattr(b, 'type', '?') for b in msg.content]})"
            )
        return "\n\n".join(parts)

    def _deepseek_news_summary(
        self, company_name: str, ticker: str,
        base_url: str = "https://api.deepseek.com", api_key: str = "",
        grounding: str = "",
    ) -> str:
        """
        DeepSeek (and Kimi/Moonshot/Gemini) have no native web-search tool, so
        we fetch headlines via NewsAPI (news_adapter.py) and ask the model to
        synthesise a narrative. `grounding`, when supplied, is the company's
        own verified latest-earnings figures (see _format_earnings_grounding)
        — NewsAPI headlines routinely lack the actual reported revenue/profit
        numbers, so this is what lets the "Latest Earnings Report" theme be
        accurate for these providers instead of vague or invented.

        Falls back to empty string only when BOTH headlines and grounding
        data are unavailable — grounding alone is enough to write a
        financial-performance theme even if NewsAPI returns nothing.
        """
        from data_sources.news_adapter import NewsAdapter
        adapter = NewsAdapter()
        articles = adapter.fetch_company_news(company_name, ticker, max_articles=12)
        headlines_block = adapter.format_for_prompt(articles)
        if not headlines_block and not grounding:
            logger.warning(
                "[LLMClient] News fallback: no NewsAPI articles and no "
                f"verified financials for {company_name} ({ticker}). "
                "Add NEWS_API_KEY to Streamlit secrets to enable headline-based news."
            )
            return ""

        source_block = (
            (f"VERIFIED LATEST EARNINGS FIGURES (use these exact numbers for the "
              f"\"Latest Earnings Report\" theme — do not alter them):\n{grounding}\n\n"
             if grounding else "")
            + (f"RECENT NEWS HEADLINES:\n{headlines_block}" if headlines_block
               else "No recent news headlines were found — base the narrative on the "
                    "verified earnings figures above only.")
        )
        summarise_prompt = (
            f"Based on the following information about {company_name} ({ticker}), "
            "write a senior equity analyst narrative overview organised into 3-5 themes "
            '(e.g. "Latest Earnings Report", "Strategic moves & M&A", "Insider activity"). '
            'Always include a "Latest Earnings Report" theme covering the most recent '
            "fiscal year/quarter's revenue, operating profit, and net income with their "
            "year-over-year % changes, plus forward guidance if available — use the "
            "verified figures below as the source of truth for these numbers. "
            "Format: each theme as **Theme name** on its own line, then 2-4 continuous "
            "prose sentences — NO bullet points, NO numbered lists, NO citation brackets. "
            "Include specific dates and numbers inline in the prose. "
            "Write each theme as ONE flowing paragraph, not fragmented lines. "
            "Do NOT include a document title or heading at the top. "
            "Plain text with **bold** theme headers only — no other markdown, no JSON.\n\n"
            f"{source_block}"
        )
        # Kimi/Gemini are "thinking" models that spend hidden reasoning tokens
        # before the visible answer — same failure class as generate()'s
        # dispatch branches. This method bypasses generate() entirely (it's
        # called directly from generate_web_news()), so the multiplier/cap/
        # timeout have to be re-applied here or the news narrative gets cut
        # off mid-sentence at a flat, unscaled 1500-token budget.
        _news_max_tokens = 1500
        _news_timeout = None
        _news_reasoning_effort = None
        if self.provider == "kimi":
            _news_max_tokens = min(int(1500 * KIMI_MAX_TOKENS_MULTIPLIER), KIMI_MAX_TOKENS_CAP)
            _news_timeout = KIMI_REQUEST_TIMEOUT_SECONDS
        elif self.provider == "gemini":
            _news_max_tokens = min(int(1500 * GEMINI_MAX_TOKENS_MULTIPLIER), GEMINI_MAX_TOKENS_CAP)
            _news_timeout = GEMINI_REQUEST_TIMEOUT_SECONDS
            _news_reasoning_effort = GEMINI_REASONING_EFFORT

        return self._openai(
            summarise_prompt, "", max_tokens=_news_max_tokens, temperature=0.3,
            base_url=base_url, api_key=api_key or DEEPSEEK_API_KEY,
            request_timeout=_news_timeout,
            reasoning_effort=_news_reasoning_effort,
        )

    def check_configured(self) -> tuple[bool, str]:
        """
        Returns (is_ready, message) to show users in the UI.
        """
        key = self._api_key()
        if not key:
            provider_label = {"claude": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY",
                              "deepseek": "DEEPSEEK_API_KEY",
                              "kimi": "MOONSHOT_API_KEY",
                              "gemini": "GEMINI_API_KEY"}.get(self.provider, "API_KEY")
            return False, (
                f"No API key found for '{self.provider}'. "
                f"Add {provider_label} to your .env file."
            )
        return True, f"Ready — {self.provider}/{self.model}"

    # ── Provider implementations ──────────────────────────────────────────────

    def _claude(
        self,
        user_prompt: str,
        system_prompt: str,
        max_tokens: int,
        temperature: float,
        cacheable_prefix: str = "",
        force_json: bool = False,
    ) -> str:
        try:
            import anthropic
        except ImportError:
            raise ImportError("Run: pip install anthropic")

        key = ANTHROPIC_API_KEY
        if not key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY not set. Add it to .env:\n"
                "  ANTHROPIC_API_KEY=sk-ant-..."
            )

        client = anthropic.Anthropic(api_key=key)

        # ── Build user content (multi-block when caching) ─────────────────────
        if cacheable_prefix:
            # Split into: [fixed framework instructions (cached)] + [variable company data]
            user_content = [
                {
                    "type": "text",
                    "text": cacheable_prefix,
                    "cache_control": {"type": "ephemeral"},
                },
                {
                    "type": "text",
                    "text": user_prompt,
                },
            ]
        else:
            user_content = user_prompt

        # ── Build system content ──────────────────────────────────────────────
        # Mark the system prompt cacheable too when we're in caching mode —
        # the combined (system + cacheable_prefix) token count is what
        # Anthropic checks against the 1024-token minimum cache threshold.
        if system_prompt and cacheable_prefix:
            system_content = [
                {
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ]
        elif system_prompt:
            system_content = system_prompt
        else:
            system_content = None

        kwargs: dict = dict(
            model=self.model,
            max_tokens=max_tokens,
            temperature=temperature,
            messages=[{"role": "user", "content": user_content}],
        )
        if system_content is not None:
            kwargs["system"] = system_content

        def _do_call(kw: dict):
            msg = client.messages.create(**kw)
            u = msg.usage
            self.last_usage = {
                "input_tokens":               u.input_tokens,
                "output_tokens":              u.output_tokens,
                "cache_creation_input_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0,
                "cache_read_input_tokens":     getattr(u, "cache_read_input_tokens",     0) or 0,
            }
            text_block = next((b for b in msg.content if hasattr(b, "text")), None)
            if text_block is None:
                raise RuntimeError("No text block in Claude response")
            return text_block.text

        try:
            return _do_call(kwargs)
        except anthropic.AuthenticationError:
            raise RuntimeError(
                "Invalid ANTHROPIC_API_KEY. Check your key at console.anthropic.com"
            )
        except anthropic.RateLimitError:
            raise RuntimeError(
                "Anthropic rate limit hit. Wait a moment and try again."
            )
        except anthropic.BadRequestError as e:
            if "temperature" in str(e):
                logger.info("[LLMClient] Model rejected temperature — retrying without it")
                kwargs.pop("temperature", None)
                try:
                    return _do_call(kwargs)
                except Exception as e2:
                    raise RuntimeError(f"Claude API error: {e2}")
            raise RuntimeError(f"Claude API error: {e}")
        except Exception as e:
            raise RuntimeError(f"Claude API error: {e}")

    def _openai(
        self,
        user_prompt: str,
        system_prompt: str,
        max_tokens: int,
        temperature: float,
        cacheable_prefix: str = "",   # prepended to user_prompt (no server-side caching for OpenAI)
        force_json: bool = False,
        base_url: str = "",
        api_key: str = "",
        request_timeout: Optional[float] = None,
        reasoning_effort: Optional[str] = None,
    ) -> str:
        try:
            from openai import OpenAI, RateLimitError
        except ImportError:
            raise ImportError("Run: pip install openai")

        key = api_key or OPENAI_API_KEY
        if not key:
            provider_hint = {
                "https://api.deepseek.com": "DEEPSEEK_API_KEY",
                "https://api.moonshot.ai/v1": "MOONSHOT_API_KEY",
                "https://generativelanguage.googleapis.com/v1beta/openai/": "GEMINI_API_KEY",
            }.get(base_url, "OPENAI_API_KEY" if not base_url else "API_KEY")
            raise RuntimeError(
                f"{provider_hint} not set. Add it to .env."
            )

        client = OpenAI(api_key=key, **({"base_url": base_url} if base_url else {}),
                        **({"timeout": request_timeout} if request_timeout else {}))
        # Prepend cacheable_prefix to user_prompt — OpenAI has no server-side
        # prompt caching via content blocks, so we just concatenate both parts
        # into one message. The full schema + instructions + data all arrive together.
        if cacheable_prefix:
            user_prompt = cacheable_prefix + "\n\n" + user_prompt
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": user_prompt})

        # DeepSeek (custom base_url) uses max_tokens; real OpenAI uses
        # max_completion_tokens on all modern models. Start with whichever
        # is appropriate and let the retry logic handle edge cases.
        _is_real_openai = not base_url
        kwargs: dict = dict(
            model=self.model,
            messages=messages,
            temperature=temperature,
        )
        if _is_real_openai:
            kwargs["max_completion_tokens"] = max_tokens
        else:
            kwargs["max_tokens"] = max_tokens
        if force_json:
            kwargs["response_format"] = {"type": "json_object"}
        if reasoning_effort:
            kwargs["reasoning_effort"] = reasoning_effort

        # Retry loop: strip unsupported params reported by the API so the
        # same code works universally across gpt-4o, gpt-4.1, gpt-5.x,
        # o-series, DeepSeek, Kimi, and any future models without hardcoding
        # lists. Also backs off and retries on 429 "rate limit / overloaded"
        # responses — these are transient (e.g. Moonshot's kimi-k3 returning
        # "engine is currently overloaded") and normally succeed a few
        # seconds later, so failing immediately wastes an otherwise-fine run.
        _rate_limit_attempts = 0
        for _attempt in range(6):
            try:
                resp = client.chat.completions.create(**kwargs)
                if resp.usage:
                    self.last_usage = {
                        "input_tokens":                resp.usage.prompt_tokens,
                        "output_tokens":               resp.usage.completion_tokens,
                        "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens":     getattr(resp.usage, "prompt_tokens_details", None)
                                                       and getattr(resp.usage.prompt_tokens_details,
                                                                   "cached_tokens", 0) or 0,
                    }
                return resp.choices[0].message.content
            except RateLimitError as e:
                if _rate_limit_attempts < 3:
                    _wait = 10 * (2 ** _rate_limit_attempts)   # 10s, 20s, 40s
                    _rate_limit_attempts += 1
                    logger.warning(
                        f"[LLMClient] Rate limited / overloaded, retrying in {_wait}s "
                        f"(attempt {_rate_limit_attempts}/3): {e}"
                    )
                    time.sleep(_wait)
                    continue
                raise RuntimeError(f"OpenAI API error: {e}")
            except Exception as e:
                err = str(e)
                # Some models reject max_tokens — swap to max_completion_tokens
                if "max_tokens" in err and "max_completion_tokens" in err:
                    if "max_tokens" in kwargs:
                        kwargs["max_completion_tokens"] = kwargs.pop("max_tokens")
                        logger.info("[LLMClient] Retrying with max_completion_tokens (model rejected max_tokens)")
                        continue
                # Some models reject non-default temperature — drop it
                if "temperature" in err and "temperature" in kwargs:
                    kwargs.pop("temperature")
                    logger.info("[LLMClient] Retrying without temperature (model rejected custom value)")
                    continue
                # Some models/endpoints reject reasoning_effort — drop it
                if "reasoning_effort" in err and "reasoning_effort" in kwargs:
                    kwargs.pop("reasoning_effort")
                    logger.info("[LLMClient] Retrying without reasoning_effort (model rejected param)")
                    continue
                raise RuntimeError(f"OpenAI API error: {e}")

    def _api_key(self) -> str:
        if self.provider == "claude":
            return ANTHROPIC_API_KEY
        elif self.provider == "openai":
            return OPENAI_API_KEY
        elif self.provider == "deepseek":
            return DEEPSEEK_API_KEY
        elif self.provider == "kimi":
            return MOONSHOT_API_KEY
        elif self.provider == "gemini":
            return GEMINI_API_KEY
        return ""


# ── Adversarial review client ─────────────────────────────────────────────────

class AdversarialReviewer:
    """
    Runs two LLM providers independently on the same analysis task,
    then has each critique the other's output. Returns a merged, higher-confidence report.

    Used when ADVERSARIAL_MODE=true in .env.
    """

    def __init__(self):
        self.primary   = LLMClient(provider="claude",  model="claude-sonnet-4-5")
        self.secondary = LLMClient(provider="openai",  model="gpt-4o")

    def generate_with_review(
        self, user_prompt: str, system_prompt: str, max_tokens: int = 4096
    ) -> dict:
        """
        Both models generate independently → each critiques the other →
        final synthesis highlights agreements and flags disagreements.

        Returns dict with keys: primary, secondary, critique_of_primary,
        critique_of_secondary, consensus_fields, contested_fields.
        """
        logger.info("[Adversarial] Running dual-model analysis…")

        # Step 1: Independent analysis (parallel would be faster, sequential for simplicity)
        primary_raw   = self.primary.generate_json(user_prompt, system_prompt, max_tokens)
        secondary_raw = self.secondary.generate_json(user_prompt, system_prompt, max_tokens)

        # Step 2: Cross-review
        critique_prompt = _build_critique_prompt(primary_raw, secondary_raw)
        critique_system = (
            "You are a senior risk analyst. Your job is to identify where two "
            "independent analyses disagree, flag overconfident claims, and note "
            "risks that one analysis missed. Be specific and cite evidence."
        )

        critique_of_primary   = self.secondary.generate(
            f"Review this analysis from Analyst A:\n{json.dumps(primary_raw, indent=2)}\n\n"
            f"Analyst B's analysis for comparison:\n{json.dumps(secondary_raw, indent=2)}\n\n"
            f"Critique Analyst A's analysis. What did they miss or overstate?",
            critique_system, 1024
        )
        critique_of_secondary = self.primary.generate(
            f"Review this analysis from Analyst B:\n{json.dumps(secondary_raw, indent=2)}\n\n"
            f"Analyst A's analysis for comparison:\n{json.dumps(primary_raw, indent=2)}\n\n"
            f"Critique Analyst B's analysis. What did they miss or overstate?",
            critique_system, 1024
        )

        # Step 3: Identify consensus vs. contested
        consensus, contested = _find_consensus_contested(primary_raw, secondary_raw)

        return {
            "primary":              primary_raw,
            "secondary":            secondary_raw,
            "critique_of_primary":  critique_of_primary,
            "critique_of_secondary": critique_of_secondary,
            "consensus_fields":     consensus,
            "contested_fields":     contested,
        }


# ── Helpers ───────────────────────────────────────────────────────────────────

def _strip_code_fences(text: str) -> str:
    """
    Remove ```json ... ``` or ``` ... ``` wrappers, even with leading whitespace
    or multiple fence variations. Also handles nested backtick content.
    """
    text = text.strip()
    # Handle ```json or ``` at the start (possibly with whitespace)
    text = re.sub(r'^`{3,}(?:json|JSON)?\s*\n?', '', text, flags=re.MULTILINE)
    # Handle ``` at the end
    text = re.sub(r'\n?`{3,}\s*$', '', text)
    return text.strip()


def _try_repair_truncated_json(text: str) -> Optional[str]:
    """
    Attempt to salvage a truncated JSON object response.

    GPT-4o / Claude can hit max_tokens mid-string when asked for long
    structured output. The response then looks like:

        {"summary": "…long text", "forces": [{"name": "Buyers", "state_…

    json.loads chokes. This helper walks the text from the end backwards,
    finds the last complete `key: value` pair, drops anything after it,
    and closes any open arrays / objects. Returns the repaired string
    if it now parses, else None.
    """
    if not text or "{" not in text:
        return None
    text = text.strip()
    # Trim trailing junk after the last brace / bracket
    last_brace = max(text.rfind("}"), text.rfind("]"))
    if last_brace < 0:
        # No closing token at all — text is a truncated string mid-value.
        # Walk back to the last ", at depth 0 or any clean quoted boundary
        # and close the open structures.
        candidate = text
    else:
        candidate = text[:last_brace + 1]

    # Walk char-by-char tracking depth + string state
    depth_obj = 0       # open { count
    depth_arr = 0       # open [ count
    in_string = False
    escaped   = False
    last_safe = -1      # index after a comma at top-of-object level
    for i, ch in enumerate(candidate):
        if escaped:
            escaped = False
            continue
        if in_string:
            if ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth_obj += 1
        elif ch == "}":
            depth_obj -= 1
        elif ch == "[":
            depth_arr += 1
        elif ch == "]":
            depth_arr -= 1
        elif ch == "," and not in_string:
            last_safe = i

    # If we end inside a string, trim back to the last comma that was
    # actually outside a string (`last_safe`, tracked during the walk
    # above) — NOT a raw rfind(",") on the text. Long-form prose (this
    # function mainly salvages verbose analyst-style JSON text fields)
    # routinely contains commas inside the string content itself (e.g.
    # "Germany, France, and Italy"); a raw rfind(",") can land on one of
    # those, cutting an already-complete field's string in half and
    # producing JSON that still can't be repaired.
    if in_string:
        if last_safe >= 0:
            candidate = candidate[:last_safe]
            depth_obj = candidate.count("{") - candidate.count("}")
            depth_arr = candidate.count("[") - candidate.count("]")
        else:
            # No safe boundary — bail out
            return None

    # Close open arrays and objects
    repaired = candidate.rstrip(", \n\t") + ("]" * max(0, depth_arr)) + ("}" * max(0, depth_obj))
    try:
        json.loads(repaired)
        return repaired
    except Exception:
        return None


def _build_critique_prompt(primary: dict, secondary: dict) -> str:
    return (
        f"Two independent analysts have produced these investment analyses.\n\n"
        f"Analyst A:\n{json.dumps(primary, indent=2)}\n\n"
        f"Analyst B:\n{json.dumps(secondary, indent=2)}\n\n"
        f"Identify: (1) Where they agree (high confidence), "
        f"(2) Where they disagree (contested — flag to investor), "
        f"(3) What risks or opportunities one raised that the other missed."
    )


def _find_consensus_contested(a: dict, b: dict) -> tuple[list, list]:
    """
    Compare recommendation fields between two analyses.
    Returns (consensus_fields, contested_fields).
    """
    consensus = []
    contested = []
    compare_keys = ["recommendation", "recommendation_rationale"]
    for key in compare_keys:
        va = a.get(key, "")
        vb = b.get(key, "")
        if isinstance(va, str) and isinstance(vb, str):
            if va.strip().lower() == vb.strip().lower():
                consensus.append(key)
            else:
                contested.append({"field": key, "primary": va[:200], "secondary": vb[:200]})
    return consensus, contested
