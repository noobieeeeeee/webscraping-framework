from __future__ import annotations

import json
from typing import Any


# Default semantic keyword groups for the German printing-configurator domain.
# Each group maps a generic "intent" label to a list of label texts that appear in the
# wild on German printing sites (Print24, Onlineprinters, Saxoprint, Viaprinto, etc.).
# The probe matches these against rendered label/heading text to find configurator
# option groups even when the underlying controls don't match standard CSS selectors.
# Callers may supply their own keyword_groups for other languages or industries.
DEFAULT_GERMAN_PRINTING_KEYWORDS: dict[str, list[str]] = {
    "menge": ["Menge", "Auflage", "Quantität", "Stückzahl", "Stück", "Anzahl"],
    "papier": ["Papier", "Papierart", "Material", "Papiersorte", "Grammatur"],
    "format": ["Format", "Größe", "Endformat", "Maße", "Abmessungen"],
    "farbigkeit": [
        "Farbe",
        "Farbig",
        "Farben",
        "Schwarzweiß",
        "Schwarz-Weiß",
        "Druckfarbe",
        "Beidseitig",
        "Einseitig",
    ],
    "seiten": ["Seiten", "Seitenzahl", "Umfang", "Innenseiten"],
    "bindung": ["Bindung", "Bindeart", "Heftung"],
    "veredelung": ["Veredelung", "Lack", "Folienkaschierung", "Prägung"],
    "ausrichtung": ["Hochformat", "Querformat", "Ausrichtung"],
}


def count_configurator_api_requests(request_urls: list[str]) -> int:
    hits = 0
    for url in request_urls:
        lowered = str(url or "").lower()
        if ("/api/" not in lowered) and ("://api." not in lowered):
            continue
        if any(token in lowered for token in ("itemmaster", "shopping-cart", "calculation", "product")):
            hits += 1
    return hits


def wait_for_configurator_readiness(
    page: Any,
    request_urls: list[str],
    timeout_ms: int,
    consent_retry_callback: Any = None,
    consent_retry_interval_ms: int = 2500,
) -> dict[str, Any]:
    # The previous cap of `min(12000, timeout_ms // 2)` made readiness time out at 10s
    # regardless of --bootstrap-timeout-ms. SPA configurators behind late-rendering
    # consent overlays (Usercentrics, Didomi) routinely need more than 10s to settle.
    # Scale with timeout_ms but keep an absolute cap so a misconfigured large timeout
    # can't stall a whole batch run.
    max_wait_ms = max(2000, min(int(timeout_ms or 0), 30000))
    waited_ms = 0
    poll_ms = 350
    reason = "timeout"
    interactive_count = 0
    consent_retries_attempted = 0
    last_consent_retry_ms = -consent_retry_interval_ms  # allow an immediate retry on first idle tick

    while waited_ms <= max_wait_ms:
        api_hits = count_configurator_api_requests(request_urls)
        try:
            interactive_count = int(
                page.evaluate(
                    r"""
                    () => {
                      const selectors = [
                      'input[type="radio"]',
                      'input[type="checkbox"]',
                      'select',
                      '[role="option"]',
                      '[role="radio"]',
                      '[role="checkbox"]',
                      '[role="tab"]',
                      '[aria-pressed]',
                      '[aria-selected]',
                      '[aria-checked]',
                      '[data-option-id]',
                      '[data-property-id]',
                      '[data-testid*="option" i]',
                      '[data-cy*="option" i]',
                      '[data-test*="option" i]',
                      ];
                      const seen = new Set();
                      selectors.forEach((s) => {
                        document.querySelectorAll(s).forEach((el) => seen.add(el));
                      });
                      return seen.size;
                    }
                    """
                )
            )
        except Exception:
            interactive_count = 0

        if interactive_count >= 2:
            reason = "interactive_controls"
            break
        if api_hits >= 2:
            reason = "pricing_api_activity"
            break

        # Deferred consent retry: Usercentrics / Didomi often render their banner only
        # after several seconds, and most SPAs gate their configurator JS on consent.
        # If we miss the banner during the initial consent attempt, the readiness loop
        # would otherwise spin until timeout with the configurator hidden behind a
        # still-unaccepted overlay. Re-running the consent helper here gives it a
        # second (and third, ...) chance once the banner has actually mounted.
        if (
            consent_retry_callback is not None
            and (waited_ms - last_consent_retry_ms) >= consent_retry_interval_ms
        ):
            try:
                consent_retry_callback(page)
            except Exception:
                pass
            consent_retries_attempted += 1
            last_consent_retry_ms = waited_ms

        try:
            page.wait_for_timeout(poll_ms)
        except Exception:
            break
        waited_ms += poll_ms

    return {
        "waitedMs": waited_ms,
        "reason": reason,
        "interactiveCount": interactive_count,
        "apiHits": count_configurator_api_requests(request_urls),
        "maxWaitMs": max_wait_ms,
        "consentRetriesAttempted": consent_retries_attempted,
    }


def attempt_cookie_consent(page: Any, auto_accept_cookies: bool) -> dict[str, Any]:
    state: dict[str, Any] = {
        "attempted": False,
        "bannerDetected": False,
        "clicked": False,
        "matchedSelector": None,
        "matchedText": None,
        "frameUrl": None,
    }
    if not auto_accept_cookies:
        return state

    state["attempted"] = True
    selector_candidates = [
        "button#onetrust-accept-btn-handler",
        "button[aria-label*='accept' i]",
        "button[title*='accept' i]",
        "button[data-testid*='accept' i]",
        "button[class*='accept' i]",
        "button[id*='accept' i]",
        "[role='button'][aria-label*='accept' i]",
        "button[aria-label*='zustimm' i]",
        "button[title*='zustimm' i]",
        "button[class*='zustimm' i]",
        "button[id*='zustimm' i]",
        # Usercentrics
        "button#uc-btn-accept-banner",
        "button[data-testid='uc-accept-all-button']",
        "button[data-testid='uc-accept-button']",
        "[data-testid*='accept-all' i]",
        # Didomi
        "#didomi-notice-agree-button",
        "button[data-testid='didomi-accept-button']",
        # Generic shadow-host markers (will not match if the button lives inside a closed shadow root;
        # the shadow-DOM walker below handles those cases.)
        "[id*='usercentrics' i] button",
        "[id*='cmp' i] button[class*='accept' i]",
    ]
    text_patterns = [
        "accept all",
        "accept",
        "allow all",
        "agree",
        "i agree",
        "alle akzeptieren",
        "akzeptieren",
        "zustimmen",
        "einverstanden",
        "alle zulassen",
    ]

    frames = list(page.frames)
    for frame in frames:
        for selector in selector_candidates:
            try:
                handle = frame.query_selector(selector)
                if handle is None:
                    continue
                state["bannerDetected"] = True
                if handle.is_visible():
                    handle.click(timeout=1000)
                    state["clicked"] = True
                    state["matchedSelector"] = selector
                    state["frameUrl"] = frame.url
                    return state
            except Exception:
                continue

        try:
            elements = frame.query_selector_all("button, [role='button'], input[type='button'], input[type='submit'], a")
        except Exception:
            elements = []

        for el in elements[:300]:
            try:
                text = (el.inner_text() or "").strip().lower()
            except Exception:
                text = ""
            if not text:
                try:
                    text = str(el.get_attribute("value") or "").strip().lower()
                except Exception:
                    text = ""
            if not text:
                continue

            if any(pattern in text for pattern in text_patterns):
                state["bannerDetected"] = True
                try:
                    el.click(timeout=1000)
                    state["clicked"] = True
                    state["matchedText"] = text[:120]
                    state["frameUrl"] = frame.url
                    return state
                except Exception:
                    continue

    # Final fallback: shadow-DOM walker. Consent vendors like Usercentrics render their
    # accept button inside nested (sometimes closed-by-default but open via mode) shadow
    # roots that frame.query_selector_all does not pierce reliably. Walking shadowRoot
    # chains in the browser, with a depth cap and deny-pattern guard, lets us click the
    # accept button without site-specific selector knowledge.
    shadow_result = _attempt_consent_via_shadow_dom(page)
    if shadow_result and shadow_result.get("clicked"):
        state["bannerDetected"] = True
        state["clicked"] = True
        state["matchedText"] = str(shadow_result.get("text") or "")[:120]
        state["matchedSelector"] = "shadow_dom_walker"
        state["frameUrl"] = str(shadow_result.get("frameUrl") or "")
    elif shadow_result and shadow_result.get("bannerDetected"):
        state["bannerDetected"] = True

    return state


def _attempt_consent_via_shadow_dom(page: Any) -> dict[str, Any] | None:
    script = r"""
    () => {
      const acceptPatterns = [
        'accept all', 'accept', 'allow all', 'agree', 'i agree',
        'alle akzeptieren', 'akzeptieren', 'zustimmen', 'einverstanden', 'alle zulassen'
      ];
      const denyPatterns = [
        'reject', 'decline', 'deny', 'ablehnen', 'verweigern', 'nur erforderliche',
        'only necessary', 'manage', 'einstellungen', 'settings', 'customize', 'anpassen'
      ];

      const isVisible = (el) => {
        if (!el) return false;
        const rect = el.getBoundingClientRect();
        if (rect.width <= 0 || rect.height <= 0) return false;
        const style = el.ownerDocument && el.ownerDocument.defaultView
          ? el.ownerDocument.defaultView.getComputedStyle(el) : null;
        if (style && (style.visibility === 'hidden' || style.display === 'none')) return false;
        return true;
      };

      const matchesAccept = (text) => {
        if (!text) return false;
        const lower = text.trim().toLowerCase();
        if (!lower) return false;
        if (denyPatterns.some((p) => lower.includes(p))) return false;
        return acceptPatterns.some((p) => lower.includes(p));
      };

      const bannerHostSelectors = [
        '[id*="usercentrics" i]', '[class*="usercentrics" i]',
        '[id*="onetrust" i]', '[id*="didomi" i]', '[id*="cookiebot" i]',
        '[id*="cmp" i]', '[class*="cookie-banner" i]', '[class*="consent" i]'
      ];
      let bannerDetected = false;

      const tryClick = (root) => {
        const candidates = root.querySelectorAll('button, [role="button"], a, input[type="button"], input[type="submit"]');
        for (const el of candidates) {
          const label = el.getAttribute('aria-label') || el.getAttribute('title') || el.textContent || el.getAttribute('value') || '';
          if (!matchesAccept(label)) continue;
          if (!isVisible(el)) continue;
          try { el.click(); } catch (_e) { continue; }
          return { clicked: true, text: (label || '').trim().slice(0, 200) };
        }
        return null;
      };

      const walk = (root, depth) => {
        if (depth > 6 || !root) return null;
        const direct = tryClick(root);
        if (direct) return direct;
        const all = root.querySelectorAll('*');
        for (const el of all) {
          if (el.shadowRoot) {
            const nested = walk(el.shadowRoot, depth + 1);
            if (nested) return nested;
          }
        }
        return null;
      };

      for (const sel of bannerHostSelectors) {
        if (document.querySelector(sel)) { bannerDetected = true; break; }
      }

      const result = walk(document, 0);
      if (result) {
        return { clicked: true, text: result.text, bannerDetected: true, frameUrl: location.href };
      }
      return { clicked: false, bannerDetected, frameUrl: location.href };
    }
    """
    try:
        return page.evaluate(script)
    except Exception:
        return None


def trigger_configurator_via_semantic_labels(
    page: Any,
    request_urls: list[str],
    keyword_groups: dict[str, list[str]] | None = None,
    max_clicks: int = 5,
    settle_ms: int = 400,
) -> dict[str, Any]:
    """Click one option per detected configurator group, located by label text.

    This is the fallback for when the conventional readiness/dependency probe finds no
    controls — typically because the configurator renders ARIA/React custom components
    whose labels match printing-domain keywords (Menge, Papier, Format, ...) but whose
    underlying controls don't match generic selectors. Clicking one option per group
    causes the configurator's JS to fire its pricing requests, which the bootstrap then
    captures via the request/response listeners installed by the caller.

    Website-agnostic: the function takes no per-site knowledge. The default keyword
    set covers German printing terms; callers can pass their own groups for other
    languages or industries.

    Returns a dict of diagnostics including which groups were detected, what was
    clicked, and the delta in observed requests / pricing API hits.
    """
    groups = dict(DEFAULT_GERMAN_PRINTING_KEYWORDS if keyword_groups is None else keyword_groups)
    requests_before = len(request_urls)
    api_hits_before = count_configurator_api_requests(request_urls)

    diagnostics: dict[str, Any] = {
        "attempted": True,
        "groupsDetected": [],
        "clicksPerformed": [],
        "requestCountBefore": requests_before,
        "requestCountAfter": requests_before,
        "requestCountDelta": 0,
        "pricingApiHitsBefore": api_hits_before,
        "pricingApiHitsAfter": api_hits_before,
        "pricingApiHitsDelta": 0,
    }

    if not groups or max_clicks <= 0:
        diagnostics["reason"] = "noop"
        return diagnostics

    script = _build_semantic_label_probe_script(groups, max_clicks)
    try:
        result = page.evaluate(script)
    except Exception as exc:
        diagnostics["error"] = f"evaluate_failed:{type(exc).__name__}"
        return diagnostics

    if isinstance(result, dict):
        diagnostics["groupsDetected"] = list(result.get("groupsDetected") or [])
        diagnostics["clicksPerformed"] = list(result.get("clicksPerformed") or [])

    if diagnostics["clicksPerformed"]:
        try:
            page.wait_for_timeout(settle_ms)
        except Exception:
            pass

    requests_after = len(request_urls)
    api_hits_after = count_configurator_api_requests(request_urls)
    diagnostics["requestCountAfter"] = requests_after
    diagnostics["requestCountDelta"] = requests_after - requests_before
    diagnostics["pricingApiHitsAfter"] = api_hits_after
    diagnostics["pricingApiHitsDelta"] = api_hits_after - api_hits_before
    return diagnostics


def _build_semantic_label_probe_script(
    keyword_groups: dict[str, list[str]],
    max_clicks: int,
) -> str:
    keywords_json = json.dumps(keyword_groups, ensure_ascii=False)
    max_clicks_int = int(max(0, max_clicks))
    return r"""
    ((keywordGroups, maxClicks) => {
      const labelSelectors = [
        'label', 'legend', 'h1', 'h2', 'h3', 'h4',
        '[role="heading"]', 'dt', 'summary',
        '[class*="label" i]', '[class*="title" i]'
      ].join(', ');
      const controlSelector = [
        '[role="radio"]', '[role="checkbox"]', '[role="tab"]', '[role="option"]',
        'input[type="radio"]', 'input[type="checkbox"]', 'select',
        '[data-option-id]', '[data-property-id]',
        '[data-testid*="option" i]', '[data-cy*="option" i]',
        'button[aria-pressed]'
      ].join(', ');

      const normalize = (s) => (s || '').toString().replace(/\s+/g, ' ').trim();
      const isVisible = (el) => {
        if (!el) return false;
        const rect = el.getBoundingClientRect();
        if (rect.width <= 0 || rect.height <= 0) return false;
        const doc = el.ownerDocument;
        const win = doc && doc.defaultView;
        if (win) {
          const style = win.getComputedStyle(el);
          if (style && (style.visibility === 'hidden' || style.display === 'none')) return false;
        }
        return true;
      };
      const isAlreadySelected = (el) => {
        if (!el) return false;
        if (el.getAttribute('aria-checked') === 'true') return true;
        if (el.getAttribute('aria-selected') === 'true') return true;
        if (el.getAttribute('aria-pressed') === 'true') return true;
        if (el.tagName === 'INPUT' && el.checked) return true;
        return false;
      };

      const allLabels = Array.from(document.querySelectorAll(labelSelectors));

      const matchedLabels = new Map();
      for (const [groupKey, keywords] of Object.entries(keywordGroups)) {
        for (const label of allLabels) {
          const text = normalize(label.textContent);
          if (!text || text.length > 80) continue;
          const lower = text.toLowerCase();
          const matched = (keywords || []).find((kw) => lower.includes(String(kw).toLowerCase()));
          if (matched) {
            matchedLabels.set(groupKey, { label, matchedKeyword: matched, labelText: text });
            break;
          }
        }
      }

      const findControlsForLabel = (label) => {
        if (label.id) {
          const safeId = label.id.replace(/(["\\\\])/g, '\\$1');
          const linked = document.querySelectorAll('[aria-labelledby~="' + safeId + '"]');
          if (linked.length > 0) return Array.from(linked);
        }
        const forAttr = label.getAttribute('for');
        if (forAttr) {
          const t = document.getElementById(forAttr);
          if (t) return [t];
        }
        let cursor = label.parentElement;
        for (let i = 0; i < 4 && cursor; i++) {
          const found = cursor.querySelectorAll(controlSelector);
          if (found.length > 0) return Array.from(found);
          cursor = cursor.parentElement;
        }
        let sib = label.nextElementSibling;
        for (let i = 0; i < 4 && sib; i++) {
          if (sib.querySelectorAll) {
            const found = sib.querySelectorAll(controlSelector);
            if (found.length > 0) return Array.from(found);
          }
          sib = sib.nextElementSibling;
        }
        return [];
      };

      const result = { groupsDetected: [], clicksPerformed: [] };
      let clicksRemaining = maxClicks;

      for (const [groupKey, info] of matchedLabels.entries()) {
        result.groupsDetected.push({
          group: groupKey,
          matchedKeyword: info.matchedKeyword,
          labelText: info.labelText,
        });
        if (clicksRemaining <= 0) continue;

        const controls = findControlsForLabel(info.label).filter(isVisible);
        if (controls.length === 0) continue;

        // Prefer a non-selected control so the click actually changes state.
        let target = controls.find((c) => !isAlreadySelected(c));
        if (!target) continue;

        if (target.tagName === 'SELECT') {
          const opts = Array.from(target.options || []);
          const currentIdx = target.selectedIndex;
          const nextIdx = opts.findIndex((opt, idx) => idx !== currentIdx && !opt.disabled);
          if (nextIdx < 0) continue;
          target.selectedIndex = nextIdx;
          try {
            target.dispatchEvent(new Event('change', { bubbles: true }));
          } catch (_e) { continue; }
          result.clicksPerformed.push({
            group: groupKey,
            matchedKeyword: info.matchedKeyword,
            labelText: info.labelText,
            clickedTag: 'select',
            clickedValue: normalize(opts[nextIdx].textContent || opts[nextIdx].value).slice(0, 80),
          });
          clicksRemaining -= 1;
          continue;
        }

        try {
          target.click();
        } catch (_e) { continue; }

        const clickedValue = normalize(
          target.getAttribute('aria-label')
          || target.getAttribute('value')
          || target.getAttribute('data-value')
          || target.textContent
        ).slice(0, 80);
        result.clicksPerformed.push({
          group: groupKey,
          matchedKeyword: info.matchedKeyword,
          labelText: info.labelText,
          clickedTag: target.tagName.toLowerCase(),
          clickedValue,
        });
        clicksRemaining -= 1;
      }

      return result;
    })(__KEYWORDS__, __MAX_CLICKS__)
    """.replace("__KEYWORDS__", keywords_json).replace("__MAX_CLICKS__", str(max_clicks_int))