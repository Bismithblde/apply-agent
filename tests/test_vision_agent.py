"""Unit tests for the Computer Use vision fallback agent."""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from agent import llm
from agent.vision_agent import (
    ACTION_SCHEMA,
    _text_matches,
    locate_and_act,
    verify_option_selected,
    vision_choose_option,
)


@pytest.fixture
def mock_page():
    page = MagicMock()
    page.viewport_size = {"width": 1280, "height": 800}
    page.screenshot.return_value = b"\xff\xd8\xff\xe0\x00\x10JFIF"  # fake jpeg
    page.mouse = MagicMock()
    page.keyboard = MagicMock()
    page.wait_for_timeout = MagicMock()
    page.wait_for_load_state = MagicMock()
    return page


class TestLocateAndAct:
    def test_click_success_coordinate_mapping(self, mock_page, monkeypatch):
        # point is [y, x] = [250, 500] in normalized 0..1000
        # px_x = 500 / 1000 * 1280 = 640
        # px_y = 250 / 1000 * 800 = 200
        monkeypatch.setattr(
            llm,
            "generate_json",
            lambda prompt, schema, **kw: {
                "action": "click",
                "point": [250, 500],
                "reason": "clicked submit button",
            },
        )

        ok = locate_and_act(mock_page, "Click the Submit button")
        assert ok is True
        mock_page.screenshot.assert_called_once_with(type="jpeg", quality=75)
        mock_page.mouse.click.assert_called_once_with(640, 200)
        mock_page.keyboard.type.assert_not_called()

    def test_custom_viewport_coordinate_mapping(self, mock_page, monkeypatch):
        mock_page.viewport_size = {"width": 1920, "height": 1080}
        # point is [y, x] = [100, 200]
        # px_x = 200 / 1000 * 1920 = 384
        # px_y = 100 / 1000 * 1080 = 108
        monkeypatch.setattr(
            llm,
            "generate_json",
            lambda prompt, schema, **kw: {
                "action": "click",
                "point": [100, 200],
                "reason": "clicked link",
            },
        )

        ok = locate_and_act(mock_page, "Click link")
        assert ok is True
        mock_page.mouse.click.assert_called_once_with(384, 108)

    def test_default_viewport_when_none(self, mock_page, monkeypatch):
        mock_page.viewport_size = None  # Playwright page without explicit viewport
        # default: width=1280, height=800
        monkeypatch.setattr(
            llm,
            "generate_json",
            lambda prompt, schema, **kw: {
                "action": "click",
                "point": [500, 500],
                "reason": "clicked center",
            },
        )

        ok = locate_and_act(mock_page, "Click center")
        assert ok is True
        mock_page.mouse.click.assert_called_once_with(640, 400)

    def test_type_action_with_text(self, mock_page, monkeypatch):
        locator = MagicMock()
        locator.bounding_box.return_value = {"x": 450, "y": 200, "width": 200, "height": 80}
        locator.evaluate.return_value = True
        mock_page.evaluate.return_value = {"dangerous": False, "optionText": ""}
        monkeypatch.setattr(
            llm,
            "generate_json",
            lambda prompt, schema, **kw: {
                "action": "type",
                "point": [300, 400],
                "text": "Jane Doe",
                "reason": "entered full name",
            },
        )

        ok = locate_and_act(mock_page, "Enter name", target_value="Jane Doe", action_hint="type",
                            locator=locator)
        assert ok is True
        # px_x = 400 / 1000 * 1280 = 512, px_y = 300 / 1000 * 800 = 240
        mock_page.mouse.click.assert_called_once_with(512, 240)
        mock_page.keyboard.type.assert_called_once_with("Jane Doe", delay=25)

    def test_type_action_fallback_to_target_value(self, mock_page, monkeypatch):
        locator = MagicMock()
        locator.bounding_box.return_value = {"x": 450, "y": 200, "width": 200, "height": 80}
        locator.evaluate.return_value = True
        mock_page.evaluate.return_value = {"dangerous": False, "optionText": ""}
        # LLM returned action "type" but text was None/empty
        monkeypatch.setattr(
            llm,
            "generate_json",
            lambda prompt, schema, **kw: {
                "action": "type",
                "point": [300, 400],
                "text": None,
                "reason": "focus input",
            },
        )

        ok = locate_and_act(mock_page, "Enter email", target_value="user@example.com", action_hint="type",
                            locator=locator)
        assert ok is True
        mock_page.mouse.click.assert_called_once_with(512, 240)
        mock_page.keyboard.type.assert_called_once_with("user@example.com", delay=25)

    def test_approved_value_overrides_model_text(self, mock_page, monkeypatch):
        locator = MagicMock()
        locator.bounding_box.return_value = {"x": 450, "y": 200, "width": 200, "height": 80}
        locator.evaluate.return_value = True
        mock_page.evaluate.return_value = {"dangerous": False, "optionText": ""}
        monkeypatch.setattr(llm, "generate_json", lambda *a, **k: {
            "action": "type", "point": [300, 400], "text": "different answer", "reason": "typed",
        })
        assert locate_and_act(mock_page, "Enter name", target_value="Approved Name", action_hint="type",
                              locator=locator)
        mock_page.keyboard.type.assert_called_once_with("Approved Name", delay=25)
        mock_page.keyboard.press.assert_called_once_with("Control+A")

    def test_out_of_bounds_coordinates_are_rejected(self, mock_page, monkeypatch):
        monkeypatch.setattr(llm, "generate_json", lambda *a, **k: {
            "action": "click", "point": [1001, 500], "reason": "invalid point",
        })
        assert not locate_and_act(mock_page, "Click target", allow_locator_fallback=False)
        mock_page.mouse.click.assert_not_called()

    def test_vision_field_action_does_not_fall_back_to_locator(self, mock_page, monkeypatch):
        locator = MagicMock()
        locator.bounding_box.return_value = {"x": 10, "y": 10, "width": 100, "height": 20}
        monkeypatch.setattr(llm, "generate_json", lambda *a, **k: {
            "action": "none", "point": [500, 500], "reason": "ambiguous",
        })
        assert not locate_and_act(mock_page, "Enter answer", target_value="value", action_hint="type",
                                  locator=locator, allow_locator_fallback=False)
        locator.bounding_box.assert_not_called()
        mock_page.mouse.click.assert_not_called()

    def test_constrained_type_action_rejects_model_click(self, mock_page, monkeypatch):
        locator = MagicMock()
        locator.bounding_box.return_value = {"x": 450, "y": 200, "width": 200, "height": 40}
        monkeypatch.setattr(llm, "generate_json", lambda *a, **k: {
            "action": "click", "point": [300, 500], "reason": "clicked another control",
        })
        assert not locate_and_act(mock_page, "Enter approved value", target_value="Jordan Lee",
                                  action_hint="type", locator=locator, allow_locator_fallback=False)
        mock_page.mouse.click.assert_not_called()
        mock_page.keyboard.type.assert_not_called()

    def test_constrained_type_action_must_hit_target_field(self, mock_page, monkeypatch):
        locator = MagicMock()
        locator.bounding_box.return_value = {"x": 500, "y": 300, "width": 200, "height": 40}
        mock_page.evaluate.return_value = {"dangerous": False, "optionText": ""}
        monkeypatch.setattr(llm, "generate_json", lambda *a, **k: {
            "action": "type", "point": [300, 400], "text": "wrong", "reason": "typed",
        })
        assert not locate_and_act(mock_page, "Enter approved value", target_value="Jordan Lee",
                                  action_hint="type", locator=locator, allow_locator_fallback=False)
        mock_page.mouse.click.assert_not_called()
        mock_page.keyboard.type.assert_not_called()

    def test_dropdown_action_cannot_hit_submit_button(self, mock_page, monkeypatch):
        mock_page.evaluate.return_value = {"dangerous": True, "optionText": "California"}
        monkeypatch.setattr(llm, "generate_json", lambda *a, **k: {
            "action": "click", "point": [300, 400], "reason": "selected California",
        })
        assert not locate_and_act(mock_page, "Select state", target_value="California",
                                  action_hint="click", allow_locator_fallback=False)
        mock_page.mouse.click.assert_not_called()

    def test_dropdown_can_open_target_listbox_button(self, mock_page, monkeypatch):
        locator = MagicMock()
        locator.bounding_box.return_value = {"x": 450, "y": 200, "width": 200, "height": 80}
        locator.evaluate.return_value = True
        mock_page.evaluate.return_value = {"dangerous": True, "safeDropdown": True, "optionText": ""}
        monkeypatch.setattr(llm, "generate_json", lambda *a, **k: {
            "action": "click", "point": [300, 500], "reason": "opened target listbox",
        })
        assert locate_and_act(mock_page, "Open state dropdown", target_value="California",
                              action_hint="click", locator=locator, allow_locator_fallback=False)
        mock_page.mouse.click.assert_called_once_with(640, 240)

    def test_action_none_returns_false(self, mock_page, monkeypatch):
        monkeypatch.setattr(
            llm,
            "generate_json",
            lambda prompt, schema, **kw: {
                "action": "none",
                "point": [0, 0],
                "reason": "element not visible on screen",
            },
        )

        ok = locate_and_act(mock_page, "Click invisible element")
        assert ok is False
        mock_page.mouse.click.assert_not_called()
        mock_page.keyboard.type.assert_not_called()

    def test_llm_unavailable_handled_gracefully(self, mock_page, monkeypatch):
        def boom(*a, **k):
            raise llm.LLMUnavailable("Network timeout or circuit open")

        monkeypatch.setattr(llm, "generate_json", boom)

        ok = locate_and_act(mock_page, "Click next button")
        assert ok is False
        mock_page.mouse.click.assert_not_called()

    def test_bad_json_handled_gracefully(self, mock_page, monkeypatch):
        def bad_json(*a, **k):
            raise json.JSONDecodeError("Expecting value", "", 0)

        monkeypatch.setattr(llm, "generate_json", bad_json)

        ok = locate_and_act(mock_page, "Click next button")
        assert ok is False
        mock_page.mouse.click.assert_not_called()

    def test_non_dict_response_handled_gracefully(self, mock_page, monkeypatch):
        monkeypatch.setattr(llm, "generate_json", lambda *a, **k: "not a dict")
        ok = locate_and_act(mock_page, "Click next button")
        assert ok is False

    def test_unknown_action_handled_gracefully(self, mock_page, monkeypatch):
        monkeypatch.setattr(
            llm,
            "generate_json",
            lambda *a, **k: {"action": "drag", "point": [10, 10], "reason": "unsupported"},
        )
        ok = locate_and_act(mock_page, "Drag something")
        assert ok is False

    def test_invalid_point_handled_gracefully(self, mock_page, monkeypatch):
        for bad_point in [[100], "not a list", None, ["not_a_num", 200]]:
            monkeypatch.setattr(
                llm,
                "generate_json",
                lambda *a, p=bad_point, **k: {
                    "action": "click",
                    "point": p,
                    "reason": "bad point",
                },
            )
            assert locate_and_act(mock_page, "Click something") is False

    def test_screenshot_failure_handled_gracefully(self, mock_page):
        mock_page.screenshot.side_effect = RuntimeError("Page crashed")
        ok = locate_and_act(mock_page, "Click something")
        assert ok is False

    def test_mouse_click_failure_handled_gracefully(self, mock_page, monkeypatch):
        mock_page.mouse.click.side_effect = RuntimeError("Frame detached")
        monkeypatch.setattr(
            llm,
            "generate_json",
            lambda *a, **k: {"action": "click", "point": [100, 100], "reason": "click"},
        )
        ok = locate_and_act(mock_page, "Click something")
        assert ok is False

    def test_locator_bounding_box_fallback_on_action_none(self, mock_page, monkeypatch):
        mock_loc = MagicMock()
        mock_loc.bounding_box.return_value = {"x": 100, "y": 200, "width": 50, "height": 30}

        monkeypatch.setattr(
            llm,
            "generate_json",
            lambda *a, **k: {"action": "none", "reason": "not found on screen"},
        )

        ok = locate_and_act(mock_page, "Open dropdown", locator=mock_loc)
        assert ok is True
        # center is x = 100 + 25 = 125, y = 200 + 15 = 215
        mock_page.mouse.click.assert_called_once_with(125, 215)

    def test_locator_bounding_box_fallback_on_llm_network_error(self, mock_page, monkeypatch):
        mock_loc = MagicMock()
        mock_loc.bounding_box.return_value = {"x": 400, "y": 300, "width": 100, "height": 40}

        def boom(*a, **k):
            raise llm.LLMUnavailable("Network disconnect")

        monkeypatch.setattr(llm, "generate_json", boom)

        ok = locate_and_act(mock_page, "Click field", locator=mock_loc)
        assert ok is True
        # center is x = 400 + 50 = 450, y = 300 + 20 = 320
        mock_page.mouse.click.assert_called_once_with(450, 320)

    def test_locator_bounding_box_fallback_on_invalid_json(self, mock_page, monkeypatch):
        mock_loc = MagicMock()
        mock_loc.bounding_box.return_value = {"x": 200, "y": 100, "width": 80, "height": 20}

        def bad_json(*a, **k):
            raise json.JSONDecodeError("Expecting value", "", 0)

        monkeypatch.setattr(llm, "generate_json", bad_json)

        ok = locate_and_act(mock_page, "Click button", locator=mock_loc)
        assert ok is True
        mock_page.mouse.click.assert_called_once_with(240, 110)


class TestVisionChooseOption:
    def test_single_step_success_with_box_locator(self, mock_page, monkeypatch):
        box_loc = MagicMock()
        box_loc.bounding_box.return_value = {"x": 500, "y": 220, "width": 40, "height": 40}
        box_loc.evaluate.return_value = True
        box_loc.inner_text.return_value = "California"
        box_loc.scroll_into_view_if_needed = MagicMock()
        mock_page.evaluate.return_value = {"dangerous": False, "optionText": "California"}

        called = []

        def fake_generate_json(prompt, schema, **kw):
            called.append(prompt)
            return {
                "action": "click",
                "point": [300, 400],
                "reason": "clicked California option",
            }

        monkeypatch.setattr(llm, "generate_json", fake_generate_json)

        ok = vision_choose_option(mock_page, box_loc, "State", "California")
        assert ok is True
        box_loc.scroll_into_view_if_needed.assert_called_once_with(timeout=3000)
        assert len(called) == 1
        mock_page.wait_for_timeout.assert_called_with(500)

    def test_two_step_flow_open_then_select(self, mock_page, monkeypatch):
        box_loc = MagicMock()
        box_loc.bounding_box.return_value = {"x": 200, "y": 50, "width": 100, "height": 50}
        box_loc.evaluate.return_value = True
        # Step 1: inner_text is unselected ("Select State...")
        # Step 2: inner_text becomes "California"
        box_loc.inner_text.side_effect = ["Select State...", "California"]
        box_loc.input_value.return_value = ""
        box_loc.get_attribute.return_value = ""
        box_loc.locator.return_value.count.return_value = 0
        mock_page.locator.return_value.count.return_value = 0
        mock_page.evaluate.return_value = ""
        mock_page.evaluate.side_effect = [
            {"dangerous": False, "optionText": ""},
            {"dangerous": False, "optionText": "California"},
        ]

        llm_responses = [
            {"action": "click", "point": [100, 200], "reason": "opened dropdown box"},
            {"action": "click", "point": [250, 200], "reason": "selected California option"},
        ]

        def fake_generate_json(prompt, schema, **kw):
            return llm_responses.pop(0)

        monkeypatch.setattr(llm, "generate_json", fake_generate_json)

        ok = vision_choose_option(mock_page, box_loc, "State", "California")
        assert ok is True
        assert len(llm_responses) == 0

    def test_box_locator_none_fails_closed(self, mock_page, monkeypatch):
        monkeypatch.setattr(
            llm,
            "generate_json",
            lambda *a, **k: {
                "action": "click",
                "point": [500, 500],
                "reason": "clicked option directly",
            },
        )
        mock_page.locator.return_value.count.return_value = 0
        mock_page.evaluate.return_value = {"dangerous": False, "optionText": "California"}

        assert not vision_choose_option(mock_page, None, "State", "California")
        mock_page.mouse.click.assert_not_called()

    def test_scroll_failure_proceeds_gracefully(self, mock_page, monkeypatch):
        box_loc = MagicMock()
        box_loc.bounding_box.return_value = {"x": 200, "y": 140, "width": 100, "height": 40}
        box_loc.evaluate.return_value = True
        box_loc.scroll_into_view_if_needed.side_effect = RuntimeError("Scroll error")
        box_loc.inner_text.return_value = "United States"
        mock_page.evaluate.return_value = {"dangerous": False, "optionText": "United States"}

        monkeypatch.setattr(
            llm,
            "generate_json",
            lambda *a, **k: {
                "action": "click",
                "point": [200, 200],
                "reason": "clicked United States",
            },
        )

        ok = vision_choose_option(mock_page, box_loc, "Country", "United States")
        assert ok is True

    def test_llm_failure_returns_false(self, mock_page, monkeypatch):
        box_loc = MagicMock()
        monkeypatch.setattr(llm, "generate_json", MagicMock(side_effect=llm.LLMUnavailable("down")))

        ok = vision_choose_option(mock_page, box_loc, "State", "California")
        assert ok is False

    def test_unverified_returns_false(self, mock_page, monkeypatch):
        box_loc = MagicMock()
        box_loc.inner_text.return_value = "Select One"
        box_loc.input_value.return_value = ""
        box_loc.get_attribute.return_value = ""
        box_loc.locator.return_value.count.return_value = 0
        mock_page.locator.return_value.count.return_value = 0
        mock_page.evaluate.return_value = ""
        mock_page.evaluate.return_value = {"dangerous": False, "optionText": "another option"}

        monkeypatch.setattr(
            llm,
            "generate_json",
            lambda *a, **k: {"action": "click", "point": [100, 100], "reason": "clicked something"},
        )

        ok = vision_choose_option(mock_page, box_loc, "State", "California", max_steps=2)
        assert ok is False


class TestVerifyOptionSelected:
    def test_empty_option_returns_false(self, mock_page):
        assert verify_option_selected(mock_page, None, "") is False

    def test_matches_via_box_inner_text(self, mock_page):
        box = MagicMock()
        box.inner_text.return_value = "United States of America"
        assert verify_option_selected(mock_page, box, "United States") is True

    def test_matches_via_box_input_value(self, mock_page):
        box = MagicMock()
        box.inner_text.return_value = ""
        box.input_value.return_value = "Full-Time"
        assert verify_option_selected(mock_page, box, "Full-Time") is True

    def test_matches_via_box_attribute_value(self, mock_page):
        box = MagicMock()
        box.inner_text.return_value = ""
        box.input_value.return_value = ""
        box.get_attribute.side_effect = lambda attr, timeout=1000: "Remote" if attr == "value" else ""
        assert verify_option_selected(mock_page, box, "Remote") is True

    def test_matches_via_box_aria_label(self, mock_page):
        box = MagicMock()
        box.inner_text.return_value = ""
        box.input_value.return_value = ""
        box.get_attribute.side_effect = lambda attr, timeout=1000: "Selected: Engineering" if attr == "aria-label" else ""
        assert verify_option_selected(mock_page, box, "Engineering") is True

    def test_matches_via_child_pill(self, mock_page):
        box = MagicMock()
        box.inner_text.return_value = ""
        box.input_value.return_value = ""
        box.get_attribute.return_value = ""

        child_item = MagicMock()
        child_item.inner_text.return_value = "Python"

        loc = MagicMock()
        loc.count.return_value = 1
        loc.nth.return_value = child_item
        box.locator.return_value = loc

        assert verify_option_selected(mock_page, box, "Python") is True

    def test_page_level_option_does_not_verify_without_target_field(self, mock_page):
        page_opt = MagicMock()
        page_opt.inner_text.return_value = "Senior Software Engineer"

        loc = MagicMock()
        loc.count.return_value = 1
        loc.nth.return_value = page_opt
        mock_page.locator.return_value = loc

        assert verify_option_selected(mock_page, None, "Senior Software Engineer") is False

    def test_option_from_other_field_does_not_verify_target(self, mock_page):
        box = MagicMock()
        box.inner_text.return_value = "Select One"
        box.input_value.return_value = ""
        box.get_attribute.return_value = ""
        box.locator.return_value.count.return_value = 0
        box.evaluate.return_value = []
        mock_page.locator.return_value.count.return_value = 1
        mock_page.locator.return_value.nth.return_value.inner_text.return_value = "California"
        assert not verify_option_selected(mock_page, box, "California")

    def test_matching_option_must_belong_to_target_dropdown(self, mock_page, monkeypatch):
        box = MagicMock()
        box.evaluate.return_value = False
        mock_page.evaluate.return_value = {"dangerous": False, "optionText": "California"}
        monkeypatch.setattr(llm, "generate_json", lambda *a, **k: {
            "action": "click", "point": [300, 400], "reason": "clicked California in another list",
        })
        assert not locate_and_act(mock_page, "Choose California", target_value="California",
                                  action_hint="click", locator=box, allow_locator_fallback=False)
        mock_page.mouse.click.assert_not_called()

    def test_text_matches_helper(self):
        assert _text_matches("United States of America", "United States") is True
        assert _text_matches("California", "california") is True
        assert _text_matches("NY", "NY") is True
        assert _text_matches("Option 1", "Option 2") is False
        assert _text_matches("", "Option") is False
        assert _text_matches(None, "Option") is False


class TestPlaywrightIntegration:
    """Live headless Playwright tests confirming real mouse and keyboard actions with vision agent."""

    def test_live_playwright_locate_and_act(self, monkeypatch):
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1000, "height": 800})
            page.set_content("""
                <html>
                <body style="margin:0; padding:0;">
                    <button id="btn" style="position:absolute; left:200px; top:100px; width:100px; height:50px;">
                        Click Me
                    </button>
                    <input id="name_input" style="position:absolute; left:200px; top:300px; width:200px; height:40px;" />
                </body>
                </html>
            """)

            # 1. Test click: button is at x=200..300, y=100..150
            # Center is x=250 (norm=250), y=125 (norm = 125/800 * 1000 = 156)
            monkeypatch.setattr(
                llm,
                "generate_json",
                lambda *a, **k: {
                    "action": "click",
                    "point": [156, 250],
                    "reason": "click button",
                },
            )
            clicked = False

            def on_click():
                nonlocal clicked
                clicked = True

            page.expose_function("btn_clicked", on_click)
            page.evaluate("document.getElementById('btn').onclick = () => window.btn_clicked()")

            assert locate_and_act(page, "Click the button") is True
            assert clicked is True

            # 2. Test type: input is at x=200..400, y=300..340
            # Center is x=300 (norm=300), y=320 (norm = 320/800 * 1000 = 400)
            monkeypatch.setattr(
                llm,
                "generate_json",
                lambda *a, **k: {
                    "action": "type",
                    "point": [400, 300],
                    "text": "Hello World",
                    "reason": "type in input",
                },
            )
            assert locate_and_act(page, "Type into input", action_hint="type") is True
            assert page.input_value("#name_input") == "Hello World"

            browser.close()

    def test_live_playwright_vision_choose_option(self, monkeypatch):
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1000, "height": 800})
            page.set_content("""
                <html>
                <body style="margin:0; padding:0;">
                    <label for="role">Role</label>
                    <input id="role" role="combobox" aria-controls="role-list" aria-expanded="true"
                      style="position:absolute; left:100px; top:50px; width:200px; height:40px;">
                    <div id="role-list" role="listbox" style="position:absolute;left:100px;top:100px">
                      <div id="role-option" role="option" style="width:200px;height:40px;"
                        onclick="document.getElementById('role').value='Software Engineer';">Software Engineer</div>
                    </div>
                    <button id="submit" style="position:absolute;left:500px;top:100px"
                      onclick="window.submitClicked=true">Submit</button>
                </body>
                </html>
            """)

            box = page.locator("#role")

            # Mock LLM clicking the option
            monkeypatch.setattr(
                llm,
                "generate_json",
                lambda *a, **k: {
                    "action": "click",
                    "point": [150, 200],  # option center: x=200, y=120
                    "reason": "click dropdown to pick Software Engineer",
                },
            )

            ok = vision_choose_option(page, box, "Role", "Software Engineer")
            assert ok is True
            assert box.input_value() == "Software Engineer"
            assert page.evaluate("window.submitClicked || false") is False

            browser.close()

