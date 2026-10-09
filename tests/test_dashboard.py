"""Focused coverage for the local dashboard and its supporting API routes."""

import importlib
import json
import shutil
import subprocess
import xml.etree.ElementTree as ET

import pytest

import db
from llm.feedback import build_feedback_report


@pytest.fixture
def dashboard_client(tmp_db, monkeypatch):
    monkeypatch.setenv("HOST", "127.0.0.1")
    monkeypatch.setenv("PORT", "9999")
    api = importlib.import_module("api")
    from web.routes import administration

    app = api.create_app({"TESTING": True, "API_TOKEN": "test-token"})
    client = app.test_client()
    client.environ_base["HTTP_AUTHORIZATION"] = "Bearer test-token"
    return administration, client


def test_dashboard_and_assets_are_served(dashboard_client):
    _api, client = dashboard_client

    page = client.get("/")
    stylesheet = client.get("/static/dashboard.css")
    script = client.get("/static/dashboard.js")
    translations = client.get("/static/dashboard-i18n.js")

    assert page.status_code == 200
    assert b"Bot Control" in page.data
    assert b"Sign in to Bot Control" in page.data
    assert b'id="login-form"' in page.data
    assert b"page-wishlist" in page.data
    assert b"page-memory" in page.data
    assert b"page-birthdays" in page.data
    assert b"sponsor-tier-form" in page.data
    assert b"sponsor-tiers-list" in page.data
    assert stylesheet.status_code == 200
    assert b"@media (max-width: 780px)" in stylesheet.data
    assert script.status_code == 200
    assert b'"X-Discord-ID-Format": "string"' in script.data
    assert b"headers.Authorization = `Bearer ${state.token}`" in script.data
    assert b'sessionStorage.getItem("bot-dashboard-token")' in script.data
    assert b"response.status === 401" in script.data
    assert b"return timestamp / 1000" in script.data
    assert b"if (!ticket.current()) return" in script.data
    assert b"requestVersions: new Map()" in script.data
    assert b"setInterval(loadStats, 15000)" in script.data
    assert b'data-metric="temperature"' in page.data
    assert b"data.temperature_celsius" in script.data
    assert b'class="keyword-groups"' in script.data
    assert b'class="keyword-group"' in script.data
    assert b"Delete response" in script.data
    assert b"Delete keyword" in script.data
    assert b">Delete all<" not in script.data
    assert b".keyword-response-text" in stylesheet.data
    assert b"/memory/channels" in script.data
    assert b"/memory/users/" in script.data
    assert b'api("/birthdays")' in script.data
    assert b"birthdayParts" in script.data
    assert b"/sponsors/tiers" in script.data
    assert b"formatChancePercent" in script.data
    assert translations.status_code == 200
    assert b"qualified_for_review" in script.data
    assert b"likes_needed" in script.data
    assert b"qualified_group_count" in script.data
    assert b"required_likes" in script.data
    assert b"recommendations" in script.data
    assert b"approval_required" in script.data
    assert b"ready_to_compare" not in script.data
    assert b"10 - group.ratings" not in script.data
    assert b"At least 10 likes in a group qualify it for administrator review." in page.data
    assert b"At least 10 ratings" not in page.data
    assert b"Administrator approval is required before applying any prompt or model change." in translations.data
    assert "Aprecieri rămase" in translations.get_data(as_text=True)


@pytest.mark.parametrize("likes,dislikes", [(0, 12), (9, 15), (10, 15)])
def test_dashboard_feedback_additive_contract(dashboard_client, likes, dislikes):
    _api, client = dashboard_client
    guild_id = 1234567890123456789
    for index in range(likes + dislikes):
        assert db.track_llm_response(
            index + 1, 7, "mention", guild_id=guild_id,
            model="discord-bot", prompt_version="v1",
        )
        assert db.set_llm_response_rating(index + 1, 7, 1 if index < likes else -1)

    response = client.get(
        f"/llm/feedback/summary?guild_id={guild_id}",
        headers={"X-Discord-ID-Format": "string"},
    )
    assert response.status_code == 200
    report = response.get_json()
    assert report["guild_id"] == str(guild_id)
    assert report["required_likes"] == 10
    assert report["qualified_group_count"] == int(likes >= 10)
    assert report["approval_required"] is True
    group = report["groups"][0]
    assert group["category"] == "mention"
    assert group["model"] == "discord-bot"
    assert group["prompt_version"] == "v1"
    assert group["ratings"] == likes + dislikes
    assert group["up"] == likes
    assert group["down"] == dislikes
    assert group["approval_percent"] == round(likes / (likes + dislikes) * 100, 1)
    assert group["ready_to_compare"] is True
    assert group["qualified_for_review"] is (likes >= 10)
    assert group["likes_needed"] == max(0, 10 - likes)
    assert group["approval_required"] is (likes >= 10)
    assert bool(group["recommendations"]) is (likes >= 10)


def test_dashboard_feedback_empty_and_retryable_api_error(dashboard_client, monkeypatch):
    _api, client = dashboard_client
    from web.routes import memory_llm

    assert client.get("/llm/feedback/summary?guild_id=88").get_json() == {
        "guild_id": 88, "required_likes": 10, "qualified_group_count": 0,
        "approval_required": True, "groups": [],
    }

    def unavailable(*_args, **_kwargs):
        raise RuntimeError("private database details")

    monkeypatch.setattr(memory_llm, "get_llm_feedback_summary", unavailable)
    response = client.get("/llm/feedback/summary?guild_id=88")
    assert response.status_code == 503
    assert response.get_json() == {
        "error": "Feedback summary is temporarily unavailable. Please try again."
    }


@pytest.fixture
def dashboard_js(dashboard_client):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is unavailable for dashboard smoke checks")
    _api, client = dashboard_client
    script = client.get("/static/dashboard.js").get_data(as_text=True)
    translations = client.get("/static/dashboard-i18n.js").get_data(as_text=True)
    harness = r"""
const vm = require("node:vm");
const input = JSON.parse(require("node:fs").readFileSync(0, "utf8"));
const nodes = new Map();
const element = (selector) => {
  if (!nodes.has(selector)) nodes.set(selector, {
    innerHTML: "", textContent: "", value: "", style: {}, dataset: {},
    classList: { add() {}, remove() {} }, append() {}, focus() {}, remove() {},
  });
  return nodes.get(selector);
};
const requests = [];
const pending = [];
let failure = false;
let hold = false;
let report = input.report;
const response = (payload, status = 200) => ({
  ok: status === 200, status,
  headers: { get: () => "application/json" }, json: async () => payload,
});
const context = vm.createContext({
  window: {}, document: {
    querySelector: element, querySelectorAll: () => [],
    createElement: () => element("toast"), addEventListener() {},
    documentElement: {},
  },
  localStorage: { getItem: (key) => key === "bot-dashboard-language" ? input.locale : "88" },
  sessionStorage: { getItem: () => "test-token", removeItem() {} },
  URLSearchParams, URL, setTimeout() {}, clearInterval() {},
  fetch: async (path, options) => {
    requests.push({path, headers: options.headers});
    if (path.startsWith("/llm/feedback/summary")) {
      if (hold) return new Promise((resolve) => pending.push(resolve));
      return failure ? response({error: input.error}, input.status || 503) : response(report);
    }
    if (path === "/llm/mention-model") return response({model: "discord-bot", allowed_models: ["discord-bot", "other-model"]});
    if (path.startsWith("/inactivity/")) return response({enabled: true});
    if (path.startsWith("/keywords/top")) return response({keywords: []});
    if (path.startsWith("/keywords/get")) return response({});
    if (path === "/system/stats") return response({cpu_percent: null, temperature_celsius: null, uptime_seconds: null});
    return response([]);
  },
});
vm.runInContext(input.translations, context);
vm.runInContext(input.script.replace(
  "window.DashboardTest = { idValue, unixSeconds, version };",
  "window.DashboardTest = { state, renderFeedback, loadOverview, loadSettings, handleAction };"
), context);
const dashboard = context.window.DashboardTest;
const target = element(`#${input.page || "overview"}-feedback`);
const load = input.page === "settings" ? dashboard.loadSettings : dashboard.loadOverview;
(async () => {
  let errorHtml = "";
  if (input.mode === "render") dashboard.renderFeedback(target, report);
  else {
    await load();
    if (input.mode === "failure") {
      failure = true;
      await load();
      errorHtml = target.innerHTML;
      failure = false;
      await dashboard.handleAction({dataset: {action: input.page === "settings" ? "load-settings" : "refresh-overview"}});
    } else if (input.mode.startsWith("switch") || input.mode.startsWith("refresh")) {
      hold = true;
      const older = load();
      for (let i = 0; i < 50 && !pending.length; i++) await Promise.resolve();
      if (!pending.length) throw new Error("The delayed feedback request was not started");
      if (input.mode.startsWith("switch")) dashboard.state.guildId = "99";
      hold = false;
      report = {...report, groups: report.groups.map((group) => ({...group, category: "current-server", recommendations: group.recommendations.map((item) => item.replaceAll("old-server", "current-server"))}))};
      if (!input.mode.includes("away")) await load();
      pending[0](input.mode.endsWith("error") ? response({error: input.error}, 503) : response(input.report));
      await older;
    } else if (input.mode === "clear") {
      dashboard.state.guildId = "";
      await load();
    }
  }
  process.stdout.write(JSON.stringify({html: target.innerHTML, error_html: errorHtml, requests, token: dashboard.state.token, model: element("#model-select").innerHTML}));
})().catch((error) => { process.stderr.write(error.stack); process.exitCode = 1; });
"""

    def run(report, **options):
        completed = subprocess.run(
            [node, "-e", harness],
            input=json.dumps({"script": script, "translations": translations, "report": report, **options}),
            text=True, capture_output=True, timeout=15,
        )
        assert completed.returncode == 0, completed.stderr
        return json.loads(completed.stdout)

    return run


@pytest.mark.parametrize("locale", ["en", "ro"])
@pytest.mark.parametrize("likes", [0, 9, 10])
def test_dashboard_feedback_likes_and_locale(dashboard_js, locale, likes):
    report = build_feedback_report([("mention", "discord-bot", "v1", likes + 15, likes, 15)])
    html = dashboard_js(report, mode="render", locale=locale)["html"]
    assert ("Administrator review" if locale == "en" else "Analiza administratorului") in html
    assert ("Qualified groups" if locale == "en" else "Grupuri calificate") + f": {int(likes >= 10)}" in html
    assert ("Likes required per group" if locale == "en" else "Aprecieri necesare pentru fiecare grup") + ": 10" in html
    assert ("before applying any prompt or model change" if locale == "en" else "înainte de aplicarea oricărei modificări de prompt sau model") in html
    handoff = (
        "Please approve a specific proposed change before it is applied. No behavior changes have been applied."
        if locale == "en" else
        "Te rugăm să aprobi o modificare concretă propusă înainte de aplicarea ei. Nu au fost aplicate modificări ale comportamentului."
    )
    assert (handoff in html) is (likes >= 10)
    if likes < 10:
        assert ("Not yet qualified" if locale == "en" else "Încă necalificat") in html
        assert ("Likes needed" if locale == "en" else "Aprecieri rămase") + f": {10 - likes}" in html
        assert "<ol>" not in html
    else:
        assert ("Qualified for administrator review" if locale == "en" else "Calificat pentru analiza administratorului") in html
        assert ("Recommendations" if locale == "en" else "Recomandări") in html
        assert ("administrator approval before applying" if locale == "en" else "aprobarea administratorului înainte de aplicare") in html
        assert ("Administrator approval required before changes." if locale == "en" else "Aprobarea administratorului este necesară înainte de modificări.") in html
        if locale == "en":
            assert "Aggregate metadata cannot diagnose individual answer failures or establish that another configuration is better." in html
        else:
            assert "Analizează evaluările pozitive și negative pentru category=mention, model=discord-bot, prompt_version=v1" in html
            assert "cere solicitanților exemple pentru analiză, deoarece textul răspunsurilor nu este stocat." in html
            assert "Compară category=mention, model=discord-bot, prompt_version=v1 cu un alt model sau o altă versiune de prompt identificată explicit" in html
            assert "când sunt disponibile evaluări comparabile, verificând numărul evaluărilor și procentele de aprobare în condiții comparabile." in html
            assert "Propune o modificare concretă a promptului sau o schimbare de model pentru category=mention, model=discord-bot, prompt_version=v1" in html
            assert "exemple justificative și un plan de evaluare" in html
            assert "Metadatele agregate conțin numărul evaluărilor și identificatorii configurațiilor, nu prompturi, textul răspunsurilor sau contextul conversației." in html
            assert "Metadatele agregate nu pot diagnostica eșecurile răspunsurilor individuale sau stabili că o altă configurație este mai bună." in html
            assert all(recommendation not in html for recommendation in report["groups"][0]["recommendations"])
        assert "<ol>" in html
    assert "Compare</th>" not in html
    assert "ready</span>" not in html


@pytest.mark.parametrize("locale", ["en", "ro"])
def test_dashboard_feedback_empty_notice(dashboard_js, locale):
    html = dashboard_js(build_feedback_report([]), mode="render", locale=locale)["html"]
    assert ("No rated replies for this server yet." if locale == "en" else "Nu există răspunsuri evaluate pentru acest server.") in html
    assert ("Qualified groups" if locale == "en" else "Grupuri calificate") + ": 0" in html
    assert "Please approve a specific proposed change" not in html
    assert "Te rugăm să aprobi o modificare concretă propusă" not in html
    assert "<table" not in html


def test_dashboard_feedback_uses_server_qualification_fields(dashboard_js):
    report = build_feedback_report([("mention", "discord-bot", "v1", 24, 9, 15)])
    report["required_likes"] = 20
    report["groups"][0]["likes_needed"] = 11
    html = dashboard_js(report, mode="render", locale="en")["html"]
    assert "Likes required per group: 20" in html
    assert "Likes needed: 11" in html
    assert "Not yet qualified" in html


@pytest.mark.parametrize("locale", ["en", "ro"])
def test_dashboard_feedback_escapes_long_labels_and_recommendations(dashboard_js, locale):
    label = '<img src=x onerror="alert(1)"> $& {configuration} {configurations}' + "long-model-" * 100
    report = build_feedback_report([(label, label, label, 10, 10, 0)])
    html = dashboard_js(report, mode="render", locale=locale)["html"]
    assert "<img" not in html
    assert "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;" in html
    assert "long-model-" * 100 in html
    assert 'title="&lt;img' in html
    assert html.count("<li>") == 4
    assert "$&amp; {configuration} {configurations}" in html
    assert ("Inspect positive and negative feedback" if locale == "en" else "Analizează evaluările pozitive și negative") in html


@pytest.mark.parametrize("locale", ["en", "ro"])
def test_dashboard_feedback_localizes_recorded_configuration_comparisons(dashboard_js, locale):
    report = build_feedback_report([
        ("mention", "model-a", "v1", 12, 10, 2),
        ("mention", "model-c", "v3", 11, 10, 1),
        ("mention", "model-b", "v2", 10, 9, 1),
        ("summon", "unrelated-model", "v4", 1, 1, 0),
    ])
    html = dashboard_js(report, mode="render", locale=locale)["html"]
    comparison = (
        "Compare category=mention, model=model-a, prompt_version=v1 against these recorded configurations: "
        if locale == "en" else
        "Compară category=mention, model=model-a, prompt_version=v1 cu aceste configurații înregistrate: "
    )
    assert comparison + "model=model-b, prompt_version=v2; model=model-c, prompt_version=v3" in html
    if locale == "ro":
        assert "verificând numărul evaluărilor și procentele de aprobare în condiții comparabile." in html
        assert all(recommendation not in html for group in report["groups"] for recommendation in group["recommendations"])


@pytest.mark.parametrize("page", ["overview", "settings"])
@pytest.mark.parametrize("locale", ["en", "ro"])
def test_dashboard_feedback_failure_replaces_stale_data_and_retries(dashboard_js, page, locale):
    report = build_feedback_report([("mention", "discord-bot", "v1", 10, 10, 0)])
    result = dashboard_js(report, mode="failure", page=page, locale=locale, error="<img src=x onerror=alert(1)>")
    assert 'role="alert"' in result["error_html"]
    assert ("Feedback could not be loaded" if locale == "en" else "Evaluările nu au putut fi încărcate") in result["error_html"]
    assert (">Retry<" if locale == "en" else ">Reîncearcă<") in result["error_html"]
    assert "<table" not in result["error_html"]
    assert "<img" not in result["error_html"]
    assert "&lt;img" in result["error_html"]
    assert "<table" in result["html"]
    assert "mention" in result["html"]
    feedback_requests = [item for item in result["requests"] if item["path"].startswith("/llm/feedback/summary")]
    assert len(feedback_requests) == 3
    assert all(item["headers"]["Authorization"] == "Bearer test-token" for item in feedback_requests)
    assert all(item["headers"]["X-Discord-ID-Format"] == "string" for item in feedback_requests)
    if page == "settings":
        assert "other-model" in result["model"]


@pytest.mark.parametrize("page", ["overview", "settings"])
@pytest.mark.parametrize("mode", ["switch-success", "switch-error", "refresh-success", "refresh-error"])
def test_dashboard_feedback_ignores_old_responses(dashboard_js, page, mode):
    report = build_feedback_report([("old-server", "discord-bot", "v1", 10, 10, 0)])
    result = dashboard_js(report, mode=mode, page=page, locale="en", error="Old request failed")
    assert "current-server" in result["html"]
    assert "old-server" not in result["html"]
    assert "Old request failed" not in result["html"]
    if mode.startswith("switch"):
        assert any("guild_id=99" in item["path"] for item in result["requests"])


@pytest.mark.parametrize("page", ["overview", "settings"])
@pytest.mark.parametrize("mode", ["switch-away-success", "switch-away-error"])
def test_dashboard_feedback_ignores_responses_after_scope_changes_on_another_page(dashboard_js, page, mode):
    report = build_feedback_report([("old-server", "discord-bot", "v1", 10, 10, 0)])
    result = dashboard_js(report, mode=mode, page=page, locale="en", error="Old request failed")
    assert "Loading feedback…" in result["html"]
    assert "old-server" not in result["html"]
    assert "Old request failed" not in result["html"]


@pytest.mark.parametrize("page", ["overview", "settings"])
def test_dashboard_feedback_clears_when_server_is_removed(dashboard_js, page):
    report = build_feedback_report([("mention", "discord-bot", "v1", 10, 10, 0)])
    result = dashboard_js(report, mode="clear", page=page, locale="en")
    assert "Enter a server ID" in result["html"]
    assert "<table" not in result["html"]


def test_birthday_navigation_uses_decorative_outline_icon(dashboard_client):
    _api, client = dashboard_client
    html = client.get("/").get_data(as_text=True)
    stylesheet = client.get("/static/dashboard.css").get_data(as_text=True)
    button_content = html.split(
        '<button class="nav-item" data-page="birthdays">', 1
    )[1].split("</button>", 1)[0]
    button = ET.fromstring(f"<button>{button_content}</button>")
    icon_slot = button.find("span")
    icon = icon_slot.find("{http://www.w3.org/2000/svg}svg")

    assert "".join(button.itertext()) == "Birthdays"
    assert "🎂" not in button_content
    assert icon_slot.attrib["class"] == "birthday-icon"
    assert icon.attrib["viewBox"] == "0 0 24 24"
    assert icon.attrib["fill"] == "none"
    assert icon.attrib["stroke"] == "currentColor"
    assert icon.attrib["aria-hidden"] == "true"
    assert icon.attrib["focusable"] == "false"
    assert icon.find("{http://www.w3.org/2000/svg}rect") is not None
    assert icon.find("{http://www.w3.org/2000/svg}path") is not None
    assert ".nav-item .birthday-icon { height: 20px;" in stylesheet
    assert "flex: 0 0 20px;" in stylesheet
    assert ".nav-item .birthday-icon svg { display: block; width: 18px; height: 18px; }" in stylesheet
    navigation_rule = stylesheet.split(".nav-item {", 1)[1].split("}", 1)[0]
    assert "color: #a7aebe;" in navigation_rule
    assert ".nav-item:hover { color: var(--text);" in stylesheet
    assert ".nav-item.active { color: white;" in stylesheet
    for selector in (".nav-item .birthday-icon {", ".nav-item .birthday-icon svg {"):
        assert "color:" not in stylesheet.split(selector, 1)[1].split("}", 1)[0]
    mobile_rules = stylesheet.split("@media (max-width: 780px) {", 1)[1]
    assert ".sidebar { transform: translateX(-100%);" in mobile_rules
    assert "body.menu-open .sidebar { transform: translateX(0); }" in mobile_rules
    assert ".menu-button { display: block; }" in mobile_rules


def test_new_data_routes_keep_bearer_auth(dashboard_client, monkeypatch):
    _administration, client = dashboard_client
    client.application.config["API_TOKEN"] = "private-token"

    assert client.get("/system/stats").status_code == 401
    assert client.get("/wishlist/history?user_id=1&url=https://example.com").status_code == 401
    assert client.get("/memory/channels?guild_id=1").status_code == 401
    assert client.get("/birthdays").status_code == 401
    assert client.get("/system/stats", headers={"Authorization": "Bearer private-token"}).status_code == 200


def test_discord_ids_accept_strings_and_round_trip_exactly(dashboard_client):
    _api, client = dashboard_client
    user_id = "1234567890123456789"
    channel_id = "8876543210987654321"

    created = client.post(
        "/reminders/add",
        json={
            "user_id": user_id,
            "channel_id": channel_id,
            "remind_at": 2_000_000_000,
            "message": "Exact snowflakes",
        },
    )
    exact = client.get(
        "/reminders/all", headers={"X-Discord-ID-Format": "string"}
    ).get_json()[0]
    legacy = client.get("/reminders/all").get_json()[0]

    assert created.status_code == 200
    assert exact["user_id"] == user_id
    assert exact["channel_id"] == channel_id
    assert legacy["user_id"] == int(user_id)
    assert legacy["channel_id"] == int(channel_id)


def test_memory_channel_and_user_controls(dashboard_client):
    _api, client = dashboard_client
    guild_id = 1234567890123456789
    channel_id = 2234567890123456789
    user_id = 3234567890123456789
    exact_headers = {"X-Discord-ID-Format": "string"}

    enabled = client.put(
        f"/memory/channels/{guild_id}/{channel_id}", json={"enabled": True}
    )
    assert enabled.status_code == 200
    channels = client.get(
        f"/memory/channels?guild_id={guild_id}", headers=exact_headers
    ).get_json()
    assert channels == {
        "guild_id": str(guild_id),
        "channels": [{"channel_id": str(channel_id), "enabled": True}],
    }

    opted_in = client.put(
        f"/memory/users/{user_id}/preference",
        json={"scope_id": str(guild_id), "enabled": True},
    )
    assert opted_in.status_code == 200
    assert db.apply_llm_memory_delta(
        guild_id,
        user_id,
        ({"kind": "fact", "content": "Likes tea", "source_text": "I like tea"},),
        (),
        (),
    )
    memory = client.get(
        f"/memory/users/{user_id}?scope_id={guild_id}", headers=exact_headers
    ).get_json()
    assert memory["scope_id"] == str(guild_id)
    assert memory["user_id"] == str(user_id)
    assert memory["preference"] is True
    assert memory["entries"][0]["content"] == "Likes tea"
    assert memory["transcript"] == []

    forgotten = client.delete(
        f"/memory/users/{user_id}", json={"scope_id": str(guild_id)}
    )
    assert forgotten.status_code == 200
    after = client.get(f"/memory/users/{user_id}?scope_id={guild_id}").get_json()
    assert after["preference"] is True
    assert after["entries"] == []
    assert after["transcript"] == []


def test_memory_opt_out_and_guild_purge_require_expected_confirmation(dashboard_client):
    _api, client = dashboard_client
    guild_id = 55
    first_user = 7
    second_user = 8
    for user_id in (first_user, second_user):
        assert db.apply_llm_memory_delta(
            guild_id,
            user_id,
            ({"kind": "topic", "content": f"Topic {user_id}", "source_text": "source"},),
            (),
            (),
        )

    opted_out = client.put(
        f"/memory/users/{first_user}/preference",
        json={"scope_id": guild_id, "enabled": False},
    )
    assert opted_out.status_code == 200
    assert opted_out.get_json()["removed"] == 1
    assert db.get_llm_memory_preference(guild_id, first_user) is False
    assert db.get_llm_memory_entries(guild_id, first_user) == []

    assert client.delete(
        f"/memory/guilds/{guild_id}", json={"confirmation": "purge"}
    ).status_code == 400
    purged = client.delete(
        f"/memory/guilds/{guild_id}", json={"confirmation": "PURGE"}
    )
    assert purged.status_code == 200
    assert purged.get_json()["removed_users"] == 1
    assert db.get_llm_memory_entries(guild_id, second_user) == []


def test_wishlist_history_is_ordered_scoped_and_handles_empty(dashboard_client):
    _api, client = dashboard_client
    owner_id = 1234567890123456789
    url = "https://shop.example/item"
    item_id = db.add_scraped_item(
        owner_id, url, title="Desk lamp", price=None, stock=True, currency="RON"
    )

    empty = client.get(
        f"/wishlist/history?user_id={owner_id}&url={url}",
        headers={"X-Discord-ID-Format": "string"},
    )
    assert empty.status_code == 200
    assert empty.get_json()["history"] == []
    assert empty.get_json()["item"]["user_id"] == str(owner_id)

    with db._connect(commit=True) as cursor:
        cursor.execute(
            "INSERT INTO price_history (item_id, price, timestamp) VALUES (?, ?, ?)",
            (item_id, 120, 200),
        )
        cursor.execute(
            "INSERT INTO price_history (item_id, price, timestamp) VALUES (?, ?, ?)",
            (item_id, 100, 100),
        )

    response = client.get(f"/wishlist/history?user_id={owner_id}&url={url}")
    assert response.status_code == 200
    assert response.get_json()["history"] == [
        {"price": 100.0, "timestamp": 100.0},
        {"price": 120.0, "timestamp": 200.0},
    ]
    assert client.get(f"/wishlist/history?user_id=9&url={url}").status_code == 404
    assert client.get(
        f"/wishlist/history?user_id={owner_id}&url=https://shop.example/missing"
    ).status_code == 404


def test_system_stats_returns_structured_metrics(dashboard_client, monkeypatch):
    api, client = dashboard_client

    class Memory:
        total = 1_000
        used = 400
        percent = 40.0

    class Disk:
        total = 2_000
        used = 500
        percent = 25.0

    monkeypatch.setattr(api.psutil, "cpu_percent", lambda interval: 12.5)
    monkeypatch.setattr(
        api.psutil,
        "sensors_temperatures",
        lambda: {"gpu": [type("Reading", (), {"current": 70.0})()], "coretemp": [type("Reading", (), {"current": 52.5})()]},
        raising=False,
    )
    monkeypatch.setattr(api.psutil, "virtual_memory", lambda: Memory())
    monkeypatch.setattr(api.psutil, "disk_usage", lambda _path: Disk())
    monkeypatch.setattr(api.psutil, "boot_time", lambda: api.time.time() - 300)

    stats = client.get("/system/stats").get_json()

    assert stats["cpu_percent"] == 12.5
    assert stats["temperature_celsius"] == 52.5
    assert stats["memory"] == {"total": 1_000, "used": 400, "percent": 40.0}
    assert stats["disk"]["percent"] == 25.0
    assert 299 <= stats["uptime_seconds"] <= 301
    assert stats["timezone"]


def test_system_stats_uses_null_for_unavailable_metrics(dashboard_client, monkeypatch):
    api, client = dashboard_client

    def unavailable(*_args, **_kwargs):
        raise OSError("not exposed")

    monkeypatch.setattr(api.psutil, "cpu_percent", unavailable)
    monkeypatch.setattr(api.psutil, "sensors_temperatures", unavailable, raising=False)
    monkeypatch.setattr(api.psutil, "virtual_memory", unavailable)
    monkeypatch.setattr(api.psutil, "disk_usage", unavailable)
    monkeypatch.setattr(api.psutil, "boot_time", unavailable)

    stats = client.get("/system/stats").get_json()

    assert stats["cpu_percent"] is None
    assert stats["temperature_celsius"] is None
    assert stats["memory"] is None
    assert stats["disk"] is None
    assert stats["uptime_seconds"] is None
