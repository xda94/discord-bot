import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
GUILD = '1234567890123456789'
USER = '2234567890123456789'


@pytest.fixture
def dashboard_js(tmp_db):
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node.js is unavailable for dashboard checks')
    harness = r'''
const vm = require("node:vm");
const input = JSON.parse(require("node:fs").readFileSync(0, "utf8"));
const nodes = new Map();
const listeners = new Map();
const element = (selector) => {
  if (!nodes.has(selector)) nodes.set(selector, {
    innerHTML: "", textContent: "", value: "", style: {}, dataset: {},
    classList: {add() {}, remove() {}}, append(node) {this.textContent = node.textContent;},
    focus() {}, remove() {}, reset() {this.fields = {};}, fields: {},
    querySelector: (query) => element(query),
    addEventListener(type, callback) {listeners.set(`${selector}:${type}`, callback);},
    elements: {namedItem: (name) => element(`field:${name}`)},
  });
  return nodes.get(selector);
};
const requests = [], pending = [], confirmations = [];
let reports = input.reports;
let hold = false, failure = false;
const response = (payload, status = 200) => ({ok:status === 200,status,headers:{get:()=>"application/json"},json:async()=>payload});
const context = vm.createContext({
  window: {}, document: {querySelector:element,querySelectorAll:()=>[],createElement:()=>element("toast"),addEventListener(){},documentElement:{}},
  localStorage:{getItem:key=>key === "bot-dashboard-language" ? input.locale : input.guild},
  sessionStorage:{getItem:()=>"test-token",removeItem(){}},
  URLSearchParams, URL, setTimeout(){},clearInterval(){},
  confirm: (message)=>{confirmations.push(message);return input.confirm !== false;},
  FormData: function(form) {return new Map(Object.entries(input.form || {}));},
  fetch: async (path, options) => {
    requests.push({path,method:options.method || "GET",body:options.body ? JSON.parse(options.body):null,headers:options.headers});
    if (options.method && options.method !== "GET") return response({status:"saved"});
    const section = path.match(/^\/community\/(\w+)\?/)[1];
    if (hold) return new Promise(resolve=>pending.push({resolve,section}));
    return failure && section === "sessions" ? response({error:"private internal <img>"},input.status || 503) : response(reports[section]);
  },
});
vm.runInContext(input.translations,context);
vm.runInContext(input.script.replace(
  "window.DashboardTest = { idValue, unixSeconds, version };",
  "window.DashboardTest = { state, loadCommunity, handleAction, saveCommunityHabit, bindForms };"
),context);
const dashboard = context.window.DashboardTest;
(async()=>{
  await dashboard.loadCommunity();
  let errorHtml = "";
  if(input.mode === "failure") {
    failure = true;await dashboard.loadCommunity();errorHtml = element("#community-sessions").innerHTML;
    failure = false;await dashboard.handleAction({dataset:{action:"load-community"}});
  } else if(input.mode === "mutation") {
    await dashboard.handleAction({dataset:{...input.dataset,guildId:input.actionGuild || input.guild}});
  } else if(input.mode === "form") {
    const form = element("#community-habit-form");
    form.dataset.guildId = input.guild;
    dashboard.bindForms();
    listeners.get("#community-habit-form:submit")({preventDefault(){},currentTarget:form});
    for(let i=0;i<80;i++) await Promise.resolve();
  } else if(input.mode.startsWith("switch") || input.mode.startsWith("refresh") || input.mode === "clear") {
    hold = true;const older = dashboard.loadCommunity();
    for(let i=0;i<50 && pending.length<4;i++) await Promise.resolve();
    if(pending.length !== 4) throw Error("Missing delayed requests");
    if(input.mode.startsWith("switch")) dashboard.state.guildId = "99";
    if(input.mode === "clear") dashboard.state.guildId = "";
    hold = false;
    reports = JSON.parse(JSON.stringify(reports).replaceAll("old-server","current-server"));
    if(!input.mode.includes("away")) await dashboard.loadCommunity();
    for(const request of pending) request.resolve(input.mode.endsWith("error") ? response({error:"old-server error"},503):response(input.reports[request.section]));
    await older;
  }
  process.stdout.write(JSON.stringify({
    html:Object.fromEntries(["sessions","bingo","habits","quotes","titles"].map(name=>[name,element(`#community-${name}`).innerHTML])),
    errorHtml,requests,confirmations,toast:element("#toast-region").textContent,
    fields:Object.fromEntries(["habit_id","text_en","text_ro"].map(name=>[name,element(`field:${name}`).value])),
    token:dashboard.state.token,
  }));
})().catch(error=>{process.stderr.write(error.stack);process.exitCode=1;});
'''
    def run(reports, **options):
        completed = subprocess.run([node, '-e', harness], input=json.dumps({'script': (ROOT / 'static/dashboard.js').read_text(), 'translations': (ROOT / 'static/dashboard-i18n.js').read_text(), 'reports': reports, 'mode': 'load', 'guild': GUILD, 'locale': 'en', **options}), text=True, capture_output=True, timeout=15)
        assert completed.returncode == 0, completed.stderr
        return json.loads(completed.stdout)
    return run


@pytest.fixture
def reports():
    return {
        'sessions': {'sessions': [{'id': 1, 'creator_id': USER, 'kind': 'sedinta', 'phase': 'active', 'payload': {'subject': 'old-server', 'options': ['One', 'Two']}, 'deadline': 1700000000, 'tally': {'0': 1, '1': 0}, 'result_text': '', 'delivery_status': 'retry', 'error_code': 'http', 'retry_at': 1700000001}]},
        'bingo': {'board': {'id': 1, 'week_key': '2026-W42', 'cells': [{'text_en': 'Habit', 'text_ro': 'Obicei'}] * 9, 'marks': [{'cell_index': 0, 'marker_id': USER, 'confirmer_id': GUILD}]}, 'habits': [{'id': 2, 'text_en': 'Habit', 'text_ro': 'Obicei'}]},
        'quotes': {'pending_count': 101, 'quotes': [{'id': 3, 'quote_text': 'A quote', 'author_name': 'Author', 'author_id': USER, 'approved_at': 1700000000}]},
        'titles': {'titles': [{'user_id': USER, 'title_key': 'hero', 'awarded_at': 1700000000}]},
    }


@pytest.mark.parametrize('locale', ['en', 'ro'])
def test_success_localization_and_exact_ids(dashboard_js, reports, locale):
    result = dashboard_js(reports, locale=locale)
    html = ' '.join(result['html'].values())
    for label in (['Cancel session', 'Retry scheduled', 'Pending author consent', 'Symbolic title', 'Confirmed'] if locale == 'en' else ['Anulează sesiunea', 'Reîncercare programată', 'Acordul autorului în așteptare', 'Titlu simbolic', 'Confirmat']):
        assert label in html
    assert USER in html
    assert GUILD in html
    assert '101' in result['html']['quotes']
    assert all(request['headers']['Authorization'] == 'Bearer test-token' for request in result['requests'])
    assert all(request['headers']['X-Discord-ID-Format'] == 'string' for request in result['requests'])
    assert all(f'guild_id={GUILD}' in request['path'] for request in result['requests'])


@pytest.mark.parametrize('locale', ['en', 'ro'])
def test_empty_and_retryable_error(dashboard_js, reports, locale):
    empty = {'sessions': {'sessions': []}, 'bingo': {'board': None, 'habits': []}, 'quotes': {'pending_count': 0, 'quotes': []}, 'titles': {'titles': []}}
    result = dashboard_js(empty, locale=locale)
    assert ('No approved quotes.' if locale == 'en' else 'Nu există citate aprobate.') in result['html']['quotes']
    assert '<table' not in ' '.join(result['html'].values())
    result = dashboard_js(reports, mode='failure', locale=locale)
    assert 'role="alert"' in result['errorHtml']
    assert ('Retry' if locale == 'en' else 'Reîncearcă') in result['errorHtml']
    assert 'private internal' not in result['errorHtml']
    assert '<table' in result['html']['sessions']
    assert len(result['requests']) == 12


@pytest.mark.parametrize('mode', ['switch-success', 'switch-error', 'switch-away-success', 'switch-away-error', 'refresh-success', 'refresh-error', 'clear'])
def test_stale_load_guard(dashboard_js, reports, mode):
    result = dashboard_js(reports, mode=mode)
    html = result['html']['sessions']
    assert 'old-server' not in html
    if mode.startswith('switch-away'):
        assert 'Loading community data' in html
    elif mode == 'clear':
        assert 'Enter a server ID above.' in html
    else:
        assert 'current-server' in html


@pytest.mark.parametrize('locale', ['en', 'ro'])
def test_escapes_all_user_content(dashboard_js, reports, locale):
    attack = '<img src=x onerror="alert(1)">'
    reports['sessions']['sessions'][0]['payload']['subject'] = attack
    reports['sessions']['sessions'][0]['payload']['options'][0] = attack
    reports['sessions']['sessions'][0]['result_text'] = attack
    reports['quotes']['quotes'][0]['quote_text'] = attack
    reports['quotes']['quotes'][0]['author_name'] = attack
    reports['titles']['titles'][0]['title_key'] = attack
    for cell in reports['bingo']['board']['cells'] + reports['bingo']['habits']:
        cell.update(text_en=attack, text_ro=attack)
    html = ' '.join(dashboard_js(reports, locale=locale)['html'].values())
    assert '<img' not in html
    assert html.count('&lt;img') >= 14


@pytest.mark.parametrize('action,path,method,dataset', [
    ('cancel-community-session', '/community/sessions/1/cancel', 'POST', {'id': '1'}),
    ('delete-community-habit', '/community/bingo/habits/2', 'DELETE', {'id': '2'}),
    ('delete-community-quote', '/community/quotes/3', 'DELETE', {'id': '3'}),
    ('delete-community-title', f'/community/titles/{USER}/hero', 'DELETE', {'userId': USER, 'titleKey': 'hero'}),
])
def test_confirmed_mutation_routes(dashboard_js, reports, action, path, method, dataset):
    result = dashboard_js(reports, mode='mutation', dataset={'action': action, **dataset}, locale='ro')
    mutations = [request for request in result['requests'] if request['method'] != 'GET']
    assert mutations == [{'path': path, 'method': method, 'body': {'guild_id': GUILD}, 'headers': {'X-Discord-ID-Format': 'string', 'Authorization': 'Bearer test-token', 'Content-Type': 'application/json'}}]
    assert GUILD in result['confirmations'][0]
    assert 'din serverul' in result['confirmations'][0]
    cancelled = dashboard_js(reports, mode='mutation', dataset={'action': action, **dataset}, confirm=False)
    assert all(request['method'] == 'GET' for request in cancelled['requests'])
    wrong_guild = dashboard_js(reports, mode='mutation', dataset={'action': action, **dataset}, actionGuild='99')
    assert all(request['method'] == 'GET' for request in wrong_guild['requests'])


@pytest.mark.parametrize('habit_id,method,path', [('', 'POST', '/community/bingo/habits'), ('2', 'PUT', '/community/bingo/habits/2')])
def test_bound_habit_form(dashboard_js, reports, habit_id, method, path):
    result = dashboard_js(reports, mode='form', form={'habit_id': habit_id, 'text_en': 'English', 'text_ro': 'Română'})
    mutation = next(request for request in result['requests'] if request['method'] != 'GET')
    assert mutation['path'] == path
    assert mutation['method'] == method
    assert mutation['body'] == {'guild_id': GUILD, 'text_en': 'English', 'text_ro': 'Română'}
    result = dashboard_js(reports, mode='mutation', dataset={'action': 'edit-community-habit', 'id': '2'})
    assert result['fields'] == {'habit_id': '2', 'text_en': 'Habit', 'text_ro': 'Obicei'}


@pytest.mark.parametrize('phase', ['pending', 'active', 'verification', 'closing', 'closed', 'cancelled', 'declined', 'expired'])
def test_all_phases_translated(dashboard_js, reports, phase):
    labels = {'pending': 'În așteptare', 'active': 'Activ', 'verification': 'Verificare', 'closing': 'În curs de închidere', 'closed': 'Închis', 'cancelled': 'Anulat', 'declined': 'Refuzat', 'expired': 'Expirat'}
    reports['sessions']['sessions'][0]['phase'] = phase
    html = dashboard_js(reports, locale='ro')['html']['sessions']
    assert labels[phase] in html
    assert ('data-action="cancel-community-session"' in html) is (phase in ['pending', 'active', 'verification'])


def test_page_guide_and_translation_catalog(tmp_db):
    source = (ROOT / 'templates/dashboard.html').read_text()
    assert source.count('data-page="community"') == 1
    assert 'id="page-community"' in source
    for command in ['tribunal', 'sedinta', 'quest', 'bingo', 'citat', 'cine-a-zis', 'scuza', 'merita', 'horoscop']:
        assert f'<code>/{command}</code>' in source
    translations = (ROOT / 'static/dashboard-i18n.js').read_text()
    assert 'Jocuri de comunitate' in translations
    assert 'window.DashboardTest = { idValue, unixSeconds, version };' in (ROOT / 'static/dashboard.js').read_text()


@pytest.mark.parametrize('error_code,label', [('not_found', 'Mesajul nu mai există'), ('forbidden', 'Mesajul nu poate fi modificat'), ('http', 'Discord este temporar indisponibil'), ('database', 'Baza de date este temporar indisponibilă'), ('cancelled', 'Anulat'), ('quote_removed', 'Citat eliminat')])
def test_delivery_errors_localized(dashboard_js, reports, error_code, label):
    reports['sessions']['sessions'][0].update(delivery_status='error', error_code=error_code)
    html = dashboard_js(reports, locale='ro')['html']['sessions']
    assert 'Trimitere eșuată' in html
    assert label in html


def test_quiz_author_choice_ids_remain_strings(dashboard_js, reports):
    reports['sessions']['sessions'][0].update(kind='quiz', payload={'quote_text': 'Quote', 'candidates': [{'user_id': USER, 'name': '<Author>'}, {'user_id': GUILD, 'name': 'Other'}]}, tally={USER: 2, GUILD: 1})
    html = dashboard_js(reports)['html']['sessions']
    assert '&lt;Author&gt;: 2' in html
    assert 'Other: 1' in html
    assert '<Author>' not in html
