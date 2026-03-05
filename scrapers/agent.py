"""AI-powered scraper using Claude to navigate dealer service schedulers."""

import asyncio
import base64
import io

import anthropic
from PIL import Image

from config import (
    PAGE_TIMEOUT,
    ACTION_TIMEOUT,
    AGENT_MODEL,
    AGENT_MAX_TURNS,
    SCREENSHOT_WIDTH,
    SCREENSHOT_HEIGHT,
)
from scrapers.base import BaseScraper, BlockedError

TOOLS = [
    {
        "name": "screenshot",
        "description": (
            "Take a screenshot of the current page. Returns a base64-encoded PNG image. "
            "Use this to see what is currently on screen."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "click",
        "description": (
            "Click on an element identified by its visible text content. "
            "Searches the main page and all iframes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "The visible text of the element to click",
                },
                "element_type": {
                    "type": "string",
                    "enum": ["link", "button", "text", "any"],
                    "description": "Type of element to look for (default: any)",
                },
            },
            "required": ["text"],
        },
    },
    {
        "name": "fill_field",
        "description": (
            "Type text into an input field identified by its label, placeholder, or nearby text."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "identifier": {
                    "type": "string",
                    "description": "The label, placeholder text, or aria-label of the input field",
                },
                "value": {
                    "type": "string",
                    "description": "The text to type into the field",
                },
            },
            "required": ["identifier", "value"],
        },
    },
    {
        "name": "select_option",
        "description": "Select an option from a dropdown/select element.",
        "input_schema": {
            "type": "object",
            "properties": {
                "identifier": {
                    "type": "string",
                    "description": "The label or name of the select element",
                },
                "option_text": {
                    "type": "string",
                    "description": "The visible text of the option to select",
                },
            },
            "required": ["identifier", "option_text"],
        },
    },
    {
        "name": "scroll",
        "description": "Scroll the page up or down.",
        "input_schema": {
            "type": "object",
            "properties": {
                "direction": {"type": "string", "enum": ["up", "down"]},
                "amount": {
                    "type": "integer",
                    "description": "Pixels to scroll (default: 500)",
                },
            },
            "required": ["direction"],
        },
    },
    {
        "name": "get_page_text",
        "description": (
            "Get the visible text content of the current page. "
            "Useful for reading text that may be hard to see in screenshots."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "wait",
        "description": "Wait for a specified number of seconds for page content to load.",
        "input_schema": {
            "type": "object",
            "properties": {
                "seconds": {
                    "type": "number",
                    "description": "Seconds to wait (1-10)",
                }
            },
            "required": ["seconds"],
        },
    },
    {
        "name": "report_result",
        "description": (
            "Report the final result of the appointment search. "
            "Call this when you have found the earliest appointment or cannot proceed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["success", "blocked", "error"],
                    "description": "success=appointment found, blocked=captcha/login wall, error=other failure",
                },
                "earliest_date": {
                    "type": "string",
                    "description": "The earliest available date (e.g., 'March 15, 2026')",
                },
                "earliest_time": {
                    "type": "string",
                    "description": "The earliest available time (e.g., '9:00 AM')",
                },
                "error": {
                    "type": "string",
                    "description": "Error description if status is not success",
                },
            },
            "required": ["status"],
        },
    },
]

SYSTEM_PROMPT = """\
You are a web scraping agent navigating a car dealer's service scheduling website.
Your goal is to find the earliest available oil change appointment.

You have a browser open to the dealer's service scheduling page. Use the tools
provided to interact with the page. Start by taking a screenshot to see what's
on screen.

Steps to follow:
1. Take a screenshot to see the current page state.
2. If there is a VIN input field, enter this VIN: {vin}
3. If the page asks for vehicle info without a VIN field, use:
   Year: 2020, Make: Volkswagen, Model: Atlas
4. Look for and select "Oil Change" or a similar maintenance/service option.
5. Navigate through any intermediate steps (advisor selection, transportation, etc.)
   by picking the first available option or skipping where possible.
6. Find the calendar/date picker showing available appointments.
7. Identify the earliest available date and time slot.
8. Call report_result with the earliest appointment details.

Important rules:
- If you encounter a captcha, login wall, or phone/email verification requirement,
  call report_result with status="blocked" and describe the blocker.
- If you get stuck after 3 attempts on the same step, call report_result with
  status="error" and explain what happened.
- Always take a screenshot after each action to verify the result.
- Look for "Continue as Guest", "Skip", or "No thanks" options to bypass sign-in.
"""


class AgentScraper(BaseScraper):
    """AI-powered scraper that uses Claude to navigate any dealer scheduler."""

    def __init__(self, page, vin, headless=False, model=None):
        super().__init__(page, vin, headless)
        self.model = model or AGENT_MODEL
        self.client = anthropic.Anthropic()

    async def _scrape_scheduler(self, dealer):
        """Use Claude to navigate the dealer's scheduler and find appointments."""
        await self.page.goto(
            dealer["url"], timeout=PAGE_TIMEOUT, wait_until="networkidle"
        )

        messages = [
            {
                "role": "user",
                "content": (
                    f"Navigate this dealer's website ({dealer['name']}) to find the "
                    "earliest oil change appointment. Start by taking a screenshot."
                ),
            }
        ]

        for _turn in range(AGENT_MAX_TURNS):
            response = self.client.messages.create(
                model=self.model,
                max_tokens=4096,
                system=SYSTEM_PROMPT.format(vin=self.vin),
                tools=TOOLS,
                messages=messages,
            )

            assistant_content = response.content
            messages.append({"role": "assistant", "content": assistant_content})

            # If Claude stopped without tool calls, it's done
            if response.stop_reason == "end_turn":
                raise Exception("Agent ended without calling report_result")

            # Process tool calls
            tool_results = []
            for block in assistant_content:
                if block.type != "tool_use":
                    continue

                # Handle the terminal report_result tool
                if block.name == "report_result":
                    inp = block.input
                    status = inp.get("status", "error")
                    if status == "success":
                        return (
                            inp.get("earliest_date", ""),
                            inp.get("earliest_time", ""),
                        )
                    elif status == "blocked":
                        raise BlockedError(
                            inp.get("error", "Blocked by login/captcha")
                        )
                    else:
                        raise Exception(inp.get("error", "Agent reported error"))

                # Execute other tools
                result = await self._execute_tool(block.name, block.input)
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    }
                )

            messages.append({"role": "user", "content": tool_results})

        raise Exception("Agent exceeded maximum turns")

    async def _execute_tool(self, name, inp):
        """Execute a tool call and return the result content."""
        if name == "screenshot":
            return await self._tool_screenshot()
        elif name == "click":
            return await self._tool_click(
                inp["text"], inp.get("element_type", "any")
            )
        elif name == "fill_field":
            return await self._tool_fill(inp["identifier"], inp["value"])
        elif name == "select_option":
            return await self._tool_select(inp["identifier"], inp["option_text"])
        elif name == "scroll":
            return await self._tool_scroll(
                inp["direction"], inp.get("amount", 500)
            )
        elif name == "get_page_text":
            return await self._tool_get_text()
        elif name == "wait":
            seconds = min(max(inp.get("seconds", 2), 1), 10)
            await asyncio.sleep(seconds)
            return [{"type": "text", "text": f"Waited {seconds} seconds."}]
        else:
            return [{"type": "text", "text": f"Unknown tool: {name}"}]

    async def _tool_screenshot(self):
        """Take a screenshot, resize it, and return as base64 image."""
        screenshot_bytes = await self.page.screenshot(type="png")
        img = Image.open(io.BytesIO(screenshot_bytes))
        img = img.resize((SCREENSHOT_WIDTH, SCREENSHOT_HEIGHT), Image.LANCZOS)
        buffer = io.BytesIO()
        img.save(buffer, format="PNG")
        b64 = base64.b64encode(buffer.getvalue()).decode()
        return [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": b64,
                },
            }
        ]

    async def _tool_click(self, text, element_type="any"):
        """Click an element by text, searching across the page and all iframes."""
        contexts = [self.page] + self.page.frames

        for ctx in contexts:
            try:
                if element_type == "button":
                    locator = ctx.get_by_role("button", name=text)
                elif element_type == "link":
                    locator = ctx.get_by_role("link", name=text)
                else:
                    locator = ctx.get_by_text(text, exact=False)

                if await locator.first.is_visible(timeout=3000):
                    await locator.first.click(timeout=ACTION_TIMEOUT)
                    await self.page.wait_for_timeout(1000)
                    return [{"type": "text", "text": f"Clicked on '{text}'."}]
            except Exception:
                continue

        return [
            {"type": "text", "text": f"Could not find clickable element with text '{text}'."}
        ]

    async def _tool_fill(self, identifier, value):
        """Fill an input field by label/placeholder, searching across frames."""
        contexts = [self.page] + self.page.frames

        for ctx in contexts:
            try:
                locator = ctx.get_by_label(identifier)
                if await locator.first.is_visible(timeout=2000):
                    await locator.first.fill(value, timeout=ACTION_TIMEOUT)
                    return [
                        {"type": "text", "text": f"Filled '{identifier}' with '{value}'."}
                    ]
            except Exception:
                pass

            try:
                locator = ctx.get_by_placeholder(identifier)
                if await locator.first.is_visible(timeout=2000):
                    await locator.first.fill(value, timeout=ACTION_TIMEOUT)
                    return [
                        {"type": "text", "text": f"Filled '{identifier}' with '{value}'."}
                    ]
            except Exception:
                continue

        return [
            {"type": "text", "text": f"Could not find input field '{identifier}'."}
        ]

    async def _tool_select(self, identifier, option_text):
        """Select a dropdown option, searching across frames."""
        contexts = [self.page] + self.page.frames

        for ctx in contexts:
            try:
                locator = ctx.get_by_label(identifier)
                if await locator.first.is_visible(timeout=2000):
                    await locator.first.select_option(
                        label=option_text, timeout=ACTION_TIMEOUT
                    )
                    return [
                        {
                            "type": "text",
                            "text": f"Selected '{option_text}' from '{identifier}'.",
                        }
                    ]
            except Exception:
                continue

        return [
            {
                "type": "text",
                "text": f"Could not find dropdown '{identifier}' or option '{option_text}'.",
            }
        ]

    async def _tool_scroll(self, direction, amount=500):
        """Scroll the page."""
        delta = amount if direction == "down" else -amount
        await self.page.mouse.wheel(0, delta)
        await self.page.wait_for_timeout(500)
        return [
            {"type": "text", "text": f"Scrolled {direction} by {amount}px."}
        ]

    async def _tool_get_text(self):
        """Get visible text from the page and iframes."""
        texts = []
        try:
            texts.append(await self.page.inner_text("body"))
        except Exception:
            pass

        for frame in self.page.frames:
            if frame == self.page.main_frame:
                continue
            try:
                text = await frame.inner_text("body")
                if text.strip():
                    texts.append(f"[iframe] {text}")
            except Exception:
                continue

        combined = "\n".join(texts)
        # Truncate to avoid huge payloads
        if len(combined) > 10000:
            combined = combined[:10000] + "\n... (truncated)"

        return [{"type": "text", "text": combined}]
