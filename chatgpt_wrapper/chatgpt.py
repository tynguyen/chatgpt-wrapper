import atexit
import base64
import json
import operator
import time
import uuid
from functools import reduce
from time import sleep
from typing import Optional
from playwright.sync_api import sync_playwright
from playwright._impl._api_structures import ProxySettings


class ChatGPT:
    """
    A ChatGPT interface that uses Playwright to run a browser,
    and interacts with that browser to communicate with ChatGPT in
    order to provide an open API to ChatGPT.
    """

    stream_div_id = "chatgpt-wrapper-conversation-stream-data"
    eof_div_id = "chatgpt-wrapper-conversation-stream-data-eof"
    session_div_id = "chatgpt-wrapper-session-data"

    def __init__(self, headless: bool = True, browser="firefox", timeout=60, proxy: Optional[ProxySettings] = None):
        self.play = sync_playwright().start()

        try:
            playbrowser = getattr(self.play, browser)
        except Exception:
            print(f"Browser {browser} is invalid, falling back on firefox")
            playbrowser = self.play.firefox

        self.browser = playbrowser.launch_persistent_context(
            user_data_dir="/tmp/playwright",
            headless=headless,
            proxy=proxy,
        )
        if len(self.browser.pages) > 0:
            self.page = self.browser.pages[0]
        else:
            self.page = self.browser.new_page()
        self._start_browser()
        self.parent_message_id = str(uuid.uuid4())
        self.conversation_id = None
        self.session = None
        self.timeout = timeout
        atexit.register(self._cleanup)

    def _start_browser(self):
        self.page.goto("https://chat.openai.com/")

    def _cleanup(self):
        self.browser.close()
        self.play.stop()

    def refresh_session(self):
        self.page.evaluate(
            """
        const xhr = new XMLHttpRequest();
        xhr.open('GET', 'https://chat.openai.com/api/auth/session');
        xhr.onload = () => {
          if(xhr.status == 200) {
            var mydiv = document.createElement('DIV');
            mydiv.id = "SESSION_DIV_ID"
            mydiv.innerHTML = xhr.responseText;
            document.body.appendChild(mydiv);
          }
        };
        xhr.send();
        """.replace(
                "SESSION_DIV_ID", self.session_div_id
            )
        )

        while True:
            session_datas = self.page.query_selector_all(f"div#{self.session_div_id}")
            if len(session_datas) > 0:
                break
            sleep(0.2)

        session_data = json.loads(session_datas[0].inner_text())
        self.session = session_data

        self.page.evaluate(f"document.getElementById('{self.session_div_id}').remove()")

    def _cleanup_divs(self):
        self.page.evaluate(f"document.getElementById('{self.stream_div_id}').remove()")
        self.page.evaluate(f"document.getElementById('{self.eof_div_id}').remove()")

    def ask_stream(self, prompt: str):
        if self.session is None:
            self.refresh_session()

        new_message_id = str(uuid.uuid4())

        if "accessToken" not in self.session:
            yield (
                "Your ChatGPT session is not usable.\n"
                "* Run this program with the `install` parameter and log in to ChatGPT.\n"
                "* If you think you are already logged in, try running the `session` command."
            )
            return

        request = {
            "messages": [
                {
                    "id": new_message_id,
                    "role": "user",
                    "content": {"content_type": "text", "parts": [prompt]},
                }
            ],
            "model": "text-davinci-002-render",
            "conversation_id": self.conversation_id,
            "parent_message_id": self.parent_message_id,
            "action": "next",
        }

        code = (
            """
            const stream_div = document.createElement('DIV');
            stream_div.id = "STREAM_DIV_ID";
            document.body.appendChild(stream_div);
            const xhr = new XMLHttpRequest();
            xhr.open('POST', 'https://chat.openai.com/backend-api/conversation');
            xhr.setRequestHeader('Accept', 'text/event-stream');
            xhr.setRequestHeader('Content-Type', 'application/json');
            xhr.setRequestHeader('Authorization', 'Bearer BEARER_TOKEN');
            xhr.responseType = 'stream';
            xhr.onreadystatechange = function() {
              var newEvent;
              if(xhr.readyState == 3 || xhr.readyState == 4) {
                const newData = xhr.response.substr(xhr.seenBytes);
                try {
                  const newEvents = newData.split(/\\n\\n/).reverse();
                  newEvents.shift();
                  if(newEvents[0] == "data: [DONE]") {
                    newEvents.shift();
                  }
                  if(newEvents.length > 0) {
                    newEvent = newEvents[0].substring(6);
                    // using XHR for eventstream sucks and occasionally ive seen incomplete
                    // json objects come through  JSON.parse will throw if that happens, and
                    // that should just skip until we get a full response.
                    JSON.parse(newEvent);
                  }
                } catch (err) {
                  console.log(err);
                  newEvent = undefined;
                }
                if(newEvent !== undefined) {
                  stream_div.innerHTML = btoa(newEvent);
                  xhr.seenBytes = xhr.responseText.length;
                }
              }
              if(xhr.readyState == 4) {
                const eof_div = document.createElement('DIV');
                eof_div.id = "EOF_DIV_ID";
                document.body.appendChild(eof_div);
              }
            };
            xhr.send(JSON.stringify(REQUEST_JSON));
            """.replace(
                "BEARER_TOKEN", self.session["accessToken"]
            )
            .replace("REQUEST_JSON", json.dumps(request))
            .replace("STREAM_DIV_ID", self.stream_div_id)
            .replace("EOF_DIV_ID", self.eof_div_id)
        )

        self.page.evaluate(code)

        last_event_msg = ""
        start_time = time.time()
        while True:
            eof_datas = self.page.query_selector_all(f"div#{self.eof_div_id}")

            conversation_datas = self.page.query_selector_all(
                f"div#{self.stream_div_id}"
            )
            if len(conversation_datas) == 0:
                continue

            full_event_message = None

            try:
                event_raw = base64.b64decode(conversation_datas[0].inner_html())
                if len(event_raw) > 0:
                    event = json.loads(event_raw)
                    if event is not None:
                        self.parent_message_id = event["message"]["id"]
                        self.conversation_id = event["conversation_id"]
                        full_event_message = "\n".join(
                            event["message"]["content"]["parts"]
                        )
            except Exception:
                yield (
                    "Failed to read response from ChatGPT.  Tips:\n"
                    " * Try again.  ChatGPT can be flaky.\n"
                    " * Use the `session` command to refresh your session, and then try again.\n"
                    " * Restart the program in the `install` mode and make sure you are logged in."
                )
                break

            if full_event_message is not None:
                chunk = full_event_message[len(last_event_msg):]
                last_event_msg = full_event_message
                yield chunk

            # if we saw the eof signal, this was the last event we
            # should process and we are done
            if len(eof_datas) > 0 or (((time.time() - start_time) > self.timeout) and full_event_message is None):
                break

            sleep(0.2)

        self._cleanup_divs()

    def ask(self, message: str) -> str:
        """
        Send a message to chatGPT and return the response.

        Args:
            message (str): The message to send.

        Returns:
            str: The response received from OpenAI.
        """
        response = list(self.ask_stream(message))
        return (
            reduce(operator.add, response)
            if len(response) > 0
            else "Unusable response produced, maybe login session expired. Try 'pkill firefox' and 'chatgpt install'"
        )

    def new_conversation(self):
        self.parent_message_id = str(uuid.uuid4())
        self.conversation_id = None
