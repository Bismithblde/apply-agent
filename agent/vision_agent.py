"""Vision-based fallback helper for Playwright pages when HTML DOM locators fail or become ambiguous."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from . import llm

if TYPE_CHECKING:
    from playwright.sync_api import Locator, Page

log = logging.getLogger(__name__)

ACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["click", "type", "none"],
            "description": "Action to perform on the page",
        },
        "point": {
            "type": "array",
            "items": {"type": "integer"},
            "description": "Normalized [y, x] coordinates in 0..1000 (y is vertical, x is horizontal)",
        },
        "text": {
            "type": ["string", "null"],
            "description": "Text to type if action is 'type'",
        },
        "reason": {
            "type": "string",
            "description": "Explanation of the decision or why action is none",
        },
    },
    "required": ["action", "reason"],
}


def _text_matches(candidate: Any, target: str) -> bool:
    if not isinstance(candidate, str) or not candidate.strip() or not target.strip():
        return False
    c = candidate.strip().lower()
    t = target.strip().lower()
    if t == c or t in c:
        return True
    if len(c) >= 3 and c in t:
        return True
    return False


def _validated_field_action(page: Page, locator: Locator | None, x: int, y: int,
                            target_value: str, action: str) -> bool:
    """Allow a visual field action only on its control or the matching option."""
    try:
        hit = page.evaluate(
            """([x, y]) => {
              const el = document.elementFromPoint(x, y);
              if (!el) return null;
              const control = el.closest('button, a, input[type=submit], input[type=button], [role=button]');
              const safeDropdown = !!control && control.tagName === 'BUTTON' &&
                (control.type || 'submit').toLowerCase() === 'button' &&
                (control.getAttribute('aria-haspopup') === 'listbox' || control.getAttribute('role') === 'combobox' ||
                 control.hasAttribute('aria-expanded'));
              const option = el.closest('[role=option], option, [aria-selected], [data-value]');
              return {dangerous: !!control && !safeDropdown, safeDropdown,
                optionText: option ? (option.innerText || option.textContent || '').trim() : ''};
            }""",
            [x, y],
        )
        if not isinstance(hit, dict):
            return False
        if hit.get("dangerous"):
            if action != "click" or not hit.get("safeDropdown") or locator is None:
                return False
            return _point_hits_locator(locator, x, y)
        if action == "click" and _text_matches(hit.get("optionText"), target_value):
            if locator is None:
                return False
            return bool(locator.evaluate(
                """(el, [x, y]) => {
                  const hit = el.ownerDocument.elementFromPoint(x, y);
                  const option = hit && hit.closest('[role=option], option, [aria-selected], [data-value]');
                  if (!option) return false;
                  const ids = `${el.getAttribute('aria-controls') || ''} ${el.getAttribute('aria-owns') || ''}`
                    .split(/\\s+/).filter(Boolean);
                  const controlled = ids.map(id => el.ownerDocument.getElementById(id)).filter(Boolean);
                  if (controlled.some(root => root === option || root.contains(option) || option.contains(root))) return true;
                  const field = el.closest('[role=combobox], [role=group], [data-automation-id^="formField-"]');
                  return !!field && field.contains(option);
                }""",
                [x, y],
            ))
        if locator is None:
            return False
        return _point_hits_locator(locator, x, y)
    except Exception as e:
        log.warning("Could not validate vision field action (%s)", type(e).__name__)
        return False


def _point_hits_locator(locator: Locator, x: int, y: int) -> bool:
    bbox = locator.bounding_box()
    if not bbox or bbox.get("width", 0) <= 0 or bbox.get("height", 0) <= 0:
        return False
    if not (bbox["x"] <= x < bbox["x"] + bbox["width"]
            and bbox["y"] <= y < bbox["y"] + bbox["height"]):
        return False
    return bool(locator.evaluate(
        """(el, [x, y]) => {
          const hit = el.ownerDocument.elementFromPoint(x, y);
          return !!hit && (hit === el || el.contains(hit) || hit.contains(el));
        }""",
        [x, y],
    ))


def _settle(page: Page, timeout_ms: int = 500) -> None:
    try:
        page.wait_for_timeout(timeout_ms)
    except Exception:
        pass
    try:
        page.wait_for_load_state("domcontentloaded", timeout=2000)
    except Exception:
        pass


def verify_option_selected(page: Page, box_locator: Locator | None, option_value: str) -> bool:
    """Verifies whether option_value is currently selected in box_locator or on page."""
    if not option_value or box_locator is None:
        return False

    if box_locator is not None:
        try:
            txt = box_locator.inner_text(timeout=1000)
            if _text_matches(txt, option_value):
                return True
        except Exception:
            pass

        # Portaled dropdowns are allowed only when the target control explicitly owns
        # the listbox that contains the selected option.
        try:
            controlled = box_locator.evaluate("""el => {
              const ids = `${el.getAttribute('aria-controls') || ''} ${el.getAttribute('aria-owns') || ''}`
                .split(/\\s+/).filter(Boolean);
              return ids.flatMap(id => {
                const root = el.ownerDocument.getElementById(id);
                return root ? [...root.querySelectorAll('[aria-selected="true"], [data-selected="true"]')]
                  .map(item => item.innerText || item.textContent || '') : [];
              });
            }""")
            if any(_text_matches(text, option_value) for text in controlled or []):
                return True
        except Exception:
            pass

        try:
            val = box_locator.input_value(timeout=1000)
            if _text_matches(val, option_value):
                return True
        except Exception:
            pass

        try:
            attr = box_locator.get_attribute("value", timeout=1000)
            if _text_matches(attr, option_value):
                return True
        except Exception:
            pass

        try:
            label = box_locator.get_attribute("aria-label", timeout=1000)
            if _text_matches(label, option_value):
                return True
        except Exception:
            pass

        try:
            selected_items = box_locator.locator(
                '[aria-selected="true"], [data-selected="true"], [class*="selected"], [class*="pill"], [class*="tag"], [class*="single-value"]'
            )
            count = selected_items.count()
            if isinstance(count, int):
                for idx in range(min(count, 10)):
                    item_text = selected_items.nth(idx).inner_text(timeout=500)
                    if _text_matches(item_text, option_value):
                        return True
        except Exception:
            pass

    try:
        active_val = box_locator.evaluate(
            """el => {
              const active = el.ownerDocument.activeElement;
              if (!active || !(active === el || el.contains(active) || active.contains(el))) return '';
              return active.value || active.innerText || '';
            }"""
        )
        if _text_matches(active_val, option_value):
            return True
    except Exception:
        pass

    return False


def locate_and_act(
    page: Page,
    goal: str,
    target_value: str | None = None,
    action_hint: str = "click",
    locator: Locator | None = None,
    context: str = "",
    allow_locator_fallback: bool = True,
) -> bool:
    """Takes a viewport screenshot, asks LLM to locate the element, maps coordinates, and acts.

    If vision fails (network error, invalid JSON, action 'none', invalid point) and a locator
    is provided, falls back to using the locator's bounding box coordinates.
    Returns True if an action was successfully executed, False otherwise.
    """
    def _fallback_to_locator() -> bool:
        if not allow_locator_fallback:
            return False
        if locator is not None:
            try:
                bbox = locator.bounding_box()
                if bbox and bbox.get("width", 0) > 0 and bbox.get("height", 0) > 0:
                    fb_x = int(bbox["x"] + bbox["width"] / 2.0)
                    fb_y = int(bbox["y"] + bbox["height"] / 2.0)
                    log.info("Vision fallback to locator bounding box at (%d, %d)", fb_x, fb_y)
                    page.mouse.click(fb_x, fb_y)
                    if action_hint == "type" and target_value:
                        page.keyboard.type(target_value, delay=25)
                    return True
            except Exception as e:
                log.warning("Locator bounding box fallback failed: %s", e)
        return False

    try:
        screenshot_bytes = page.screenshot(type="jpeg", quality=75)
    except Exception as e:
        log.warning("Vision agent failed to take screenshot: %s", e)
        return _fallback_to_locator()

    viewport_size = page.viewport_size or {"width": 1280, "height": 800}

    prompt = (
        f"You are a computer use assistant. Locate the element on the screen to accomplish the goal:\n"
        f"Goal: {goal}\n"
    )
    if target_value is not None:
        prompt += f"Target value / text: {target_value}\n"
    if context:
        prompt += f"Page and target context:\n{context[:1800]}\n"
    prompt += (
        f"Required action: {action_hint}\n"
        f"Identify the center of the target element as normalized coordinates [y, x] in range 0..1000,\n"
        f"where y=0 is top, y=1000 is bottom, x=0 is left, x=1000 is right.\n"
        f"If action is 'click', return 'action': 'click', and 'point': [y, x].\n"
        f"If action is 'type', return 'action': 'type', 'point': [y, x] (where to click/focus), and 'text': string.\n"
        f"If target is not visible or cannot be determined, return 'action': 'none'."
    )

    model = None
    try:
        model = llm.vision_model()
    except Exception:
        pass

    try:
        reply = llm.generate_json(
            prompt,
            ACTION_SCHEMA,
            images=[screenshot_bytes],
            model=model,
            temperature=0.1,
        )
    except Exception as e:
        log.warning("Vision LLM call failed: %s", e)
        return _fallback_to_locator()

    if not isinstance(reply, dict):
        log.warning("Vision LLM returned non-dict response: %r", reply)
        return _fallback_to_locator()

    action = reply.get("action")
    reason = str(reply.get("reason", ""))
    if action == "none":
        log.info("Vision LLM decided no action ('none'): %s", reason)
        return _fallback_to_locator()

    if action not in ("click", "type"):
        log.warning("Vision LLM returned unrecognized action %r: %s", action, reason)
        return _fallback_to_locator()

    point = reply.get("point")
    if not isinstance(point, (list, tuple)) or len(point) < 2:
        log.warning("Vision LLM returned invalid point: %r", point)
        return _fallback_to_locator()

    try:
        y_norm = float(point[0])
        x_norm = float(point[1])
    except (ValueError, TypeError):
        log.warning("Vision LLM returned non-numeric point: %r", point)
        return _fallback_to_locator()
    if not (0 <= y_norm < 1000 and 0 <= x_norm < 1000):
        log.warning("Vision LLM returned out-of-bounds point: %r", point)
        return _fallback_to_locator()

    if target_value is not None and action != action_hint:
        log.warning("Vision LLM returned action %r for constrained %r request", action, action_hint)
        return False

    px_x = int(x_norm / 1000.0 * viewport_size["width"])
    px_y = int(y_norm / 1000.0 * viewport_size["height"])

    # A visual field action must resolve to the target control or matching option.
    # This keeps screenshot-selected coordinates away from Submit, Next, and other fields.
    if target_value is not None and not _validated_field_action(
            page, locator, px_x, px_y, target_value, action_hint):
        log.warning("Vision point did not resolve to the target field or matching option")
        return False

    log.info("Vision action %s at (%d, %d) [norm: %s]: %s", action, px_x, px_y, point, reason)

    try:
        page.mouse.click(px_x, px_y)
        # When the caller supplies an approved answer, vision may locate the target but
        # cannot alter the answer text or its provenance.
        text = target_value if target_value is not None else reply.get("text")
        if action == "type" and text:
            if target_value is not None:
                page.keyboard.press("Control+A")
            page.keyboard.type(text, delay=25)
    except Exception as e:
        log.warning("Failed executing vision action %s at (%d, %d): %s", action, px_x, px_y, e)
        return _fallback_to_locator()

    return True


def vision_choose_option(
    page: Page,
    box_locator: Locator | None,
    field_label: str,
    option_value: str,
    max_steps: int = 2,
) -> bool:
    """Computer Use helper specifically for selecting a dropdown option when virtualized DOM fails.

    If box_locator is provided, ensures it is in view. Takes screenshot, asks LLM to find
    the option matching option_value (or the dropdown box to open it), clicks it, waits for UI
    to settle, and verifies if the option was selected.
    """
    if box_locator is not None:
        try:
            box_locator.scroll_into_view_if_needed(timeout=3000)
        except Exception as e:
            log.warning("Failed to scroll box_locator into view: %s", e)

    for step in range(max_steps):
        if step == 0:
            goal = (
                f"Select option '{option_value}' for field '{field_label}'. "
                f"If the dropdown list is already open, click '{option_value}'. "
                f"If the dropdown is closed, click the dropdown box to open it."
            )
        else:
            goal = f"Click the dropdown option '{option_value}' for field '{field_label}' in the open dropdown menu."

        ok = locate_and_act(
            page,
            goal=goal,
            target_value=option_value,
            action_hint="click",
            locator=box_locator,
        )
        if not ok:
            return False

        _settle(page)

        if verify_option_selected(page, box_locator, option_value):
            return True

    return False
