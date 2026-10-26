const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const source = fs.readFileSync(path.join(__dirname, '../webfrontend/htmlauth/miflora.js'), 'utf8');
const {inputExamples} = require('../webfrontend/htmlauth/miflora.js');

function element(properties) {
    return Object.assign({
        value: '', textContent: '', hidden: false, disabled: false,
        checked: true, scrollTop: 0, scrollHeight: 500, dataset: {}, events: {},
        options: [],
        replaceChildren() { this.options = []; },
        appendChild(option) { this.options.push(option); },
        focus() { this.focused = true; },
        select() { this.selected = true; },
        addEventListener(name, callback) { this.events[name] = callback; }
    }, properties);
}

function context(elements, controls, extra) {
    return Object.assign({
        document: {
            readyState: 'complete',
            getElementById(id) { return elements[id] || null; },
            createElement() { return element(); },
            querySelector(selector) { return controls[selector] || null; }
        },
        window: {location: {href: 'http://localhost/index.cgi?tab=status'}},
        URL, URLSearchParams, module: {exports: {}}
    }, extra);
}

function testExamples() {
    const example = inputExamples('MiFlora', '5C857EB0F3AD', 'Temperature');
    assert.equal(example.topic, 'miflora/5c857eb0f3ad/temperature');
    assert.equal(example.mqtt, 'miflora_5c857eb0f3ad_temperature');
    assert.equal(example.udp, '5C857EB0F3AD.Temperature=\\v');
    assert.equal(inputExamples('a/b% c', 'AABBCCDDEEFF', 'Battery').mqtt, 'a_b__c_aabbccddeeff_battery');

    const rows = ['Name', 'PollTime', 'PollTimeString', 'Firmware', 'Temperature', 'Moisture',
        'Light', 'Conductivity', 'Battery'].map(function (key) {
        const cells = {'.inputExample': element(), '.topicExample': element()};
        return element({dataset: {key}, querySelector(selector) { return cells[selector]; }});
    });
    const columns = [element(), element()];
    const elements = {
        mifloraMessages: element({dataset: {virtual: 'Virtual', udp: 'UDP'}}),
        loxoneInputs: element({
            dataset: {topic: 'miflora'},
            querySelectorAll(selector) { return selector === '.topicColumn' ? columns : rows; }
        }),
        sensorMac: element({value: '5C:85:7E:B0:F3:AD'}),
        inputTransport: element({value: 'mqtt'}),
        macError: element(), mqttHelp: element(), udpHelp: element(),
        inputColumn: element(), mqttSubscription: element()
    };
    vm.runInNewContext(source, context(elements, {}));
    assert.equal(rows[4].querySelector('.inputExample').textContent, example.mqtt);
    assert.equal(elements.mqttSubscription.textContent, 'miflora/#');
    assert.equal(elements.macError.hidden, true);
    elements.inputTransport.value = 'udp';
    elements.inputTransport.events.change();
    assert.equal(rows[4].querySelector('.inputExample').textContent, example.udp);
    assert.equal(rows[0].querySelector('.inputExample').textContent, '-');
    assert.equal(rows[2].querySelector('.inputExample').textContent, '-');
    assert.equal(elements.mqttHelp.hidden, true);
    assert.equal(elements.udpHelp.hidden, false);
    assert(columns.every(c => c.hidden));
    elements.sensorMac.value = 'invalid';
    elements.sensorMac.events.input();
    assert.equal(elements.macError.hidden, false);
    assert(rows.every(r => r.querySelector('.inputExample').textContent === ''));
    // Gateway v2 has no wildcard subscription element.
    delete elements.mqttSubscription;
    elements.sensorMac.value = '5C:85:7E:B0:F3:AD';
    elements.inputTransport.value = 'mqtt';
    elements.inputTransport.events.change();
    assert.equal(elements.udpHelp.hidden, true);
    assert.equal(elements.mqttHelp.hidden, false);
}

async function testLivePolling() {
    const elements = {
        mifloraMessages: element({dataset: {polling: 'Scan starting', error: 'Request failed', example: 'Example'}}),
        txtLastData: element(), dataAutoScroll: element(), liveError: element(),
        clearOutput: element(), editMac: element(),
        loxoneInputs: element({dataset: {topic: 'miflora'}, querySelectorAll() { return []; }}),
        sensorMac: element({value: '5C:85:7E:B0:F3:AD', hidden: true}),
        sensorSelect: element({options: [element({value: '5C:85:7E:B0:F3:AD'})]}),
        inputTransport: element({value: 'mqtt'}),
        macError: element(), mqttHelp: element(), udpHelp: element(), inputColumn: element()
    };
    const poll = element();
    let nextTimer;
    let pending = [];
    let starts = 0;
    let clearReply;
    vm.runInNewContext(source, context(elements, {'[name=btnPoll]': poll}, {
        setTimeout(callback, delay) { assert.equal(delay, 1000); nextTimer = callback; return 1; },
        clearTimeout() {},
        fetch(url, options) {
            if (url.searchParams.get('action') === 'poll') {
                assert.equal(options.method, 'POST');
                starts++;
                return Promise.resolve({ok: true, json: async () => ({started: true})});
            }
            if (url.searchParams.get('action') === 'clear-output') {
                assert.equal(options.method, 'POST');
                return new Promise(resolve => { clearReply = resolve; });
            }
            return new Promise(resolve => pending.push(resolve));
        }
    }));
    function reply(data) {
        pending.shift()({ok: true, json: async () => data});
    }
    const settle = () => new Promise(resolve => setImmediate(resolve));
    await poll.events.click({preventDefault() {}});
    assert.equal(elements.txtLastData.value, 'Scan starting\n');
    reply({text: 'stale values from previous query', running: false});
    await settle();
    assert.equal(elements.txtLastData.value, 'Scan starting\n');
    let updating = nextTimer();
    reply({text: 'START\nScanning USB adapter', running: true, sensors: ['AA:BB:CC:DD:EE:FF']});
    await updating;
    assert.equal(poll.disabled, true);
    assert.equal(elements.clearOutput.disabled, true);
    assert.equal(elements.sensorMac.value, 'AA:BB:CC:DD:EE:FF');
    assert.equal(elements.txtLastData.scrollTop, 500);
    await poll.events.click({preventDefault() {}});
    assert.equal(starts, 1);
    elements.dataAutoScroll.checked = false;
    elements.txtLastData.scrollTop = 123;
    updating = nextTimer();
    const sensors = ['AA:BB:CC:DD:EE:FF', '11:22:33:44:55:66'];
    reply({text: 'START\nSensor found\nSTOP OK', running: false, sensors});
    await updating;
    assert.equal(poll.disabled, false);
    assert.equal(elements.txtLastData.scrollTop, 123);
    assert(elements.txtLastData.value.includes('STOP OK'));
    assert.equal(elements.sensorSelect.options.length, 2);
    elements.sensorSelect.value = sensors[1];
    elements.sensorSelect.events.change();
    assert.equal(elements.sensorMac.value, sensors[1]);
    assert.equal(elements.sensorMac.hidden, true);
    assert.equal(elements.sensorSelect.hidden, false);
    elements.editMac.events.click();
    assert.equal(elements.sensorMac.hidden, false);
    assert.equal(elements.sensorSelect.hidden, true);
    assert.equal(elements.sensorMac.value, '5C:85:7E:B0:F3:AD');
    assert.equal(elements.sensorMac.focused, true);
    elements.sensorMac.value = '12:34:56:78:90:AB';
    elements.sensorMac.events.input();
    updating = nextTimer();
    reply({text: 'STOP OK', running: false, sensors});
    await updating;
    assert.equal(elements.sensorMac.hidden, false);
    assert.equal(elements.sensorSelect.hidden, true);
    assert.equal(elements.sensorMac.value, '12:34:56:78:90:AB');
    updating = nextTimer();
    reply({text: 'START\nScanning USB adapter', running: true, sensors: []});
    await updating;
    assert.equal(elements.sensorMac.hidden, false);
    assert.equal(elements.sensorSelect.hidden, true);
    updating = nextTimer();
    reply({text: 'START\nSensor found', running: true, sensors: [sensors[0]]});
    await updating;
    assert.equal(elements.sensorMac.hidden, true);
    assert.equal(elements.sensorSelect.hidden, false);
    assert.equal(elements.sensorSelect.value, sensors[0]);
    updating = nextTimer();
    reply({text: 'STOP OK', running: false, sensors: [sensors[0]]});
    await updating;
    updating = nextTimer();
    pending.shift()({ok: false, json: async () => ({error: 'connection lost'})});
    await updating;
    assert.equal(elements.liveError.hidden, false);
    assert(elements.liveError.textContent.includes('connection lost'));
    // A clear must discard any status request that was already in flight.
    updating = nextTimer();
    const clearing = elements.clearOutput.events.click();
    reply({text: 'old output', running: false, sensors});
    await updating;
    clearReply({ok: true, json: async () => ({text: '', sensors: []})});
    await clearing;
    assert.equal(elements.txtLastData.value, '');
    assert.equal(elements.sensorMac.value, '5C:85:7E:B0:F3:AD');
    assert.equal(elements.sensorMac.hidden, true);
    assert.equal(elements.sensorSelect.options.length, 1);
    assert.equal(elements.sensorSelect.options[0].textContent, '5C:85:7E:B0:F3:AD (Example)');
    reply({text: '', running: false, sensors: []});
    await settle();
    assert.equal(elements.txtLastData.value, '');
    elements.editMac.events.click();
    elements.sensorMac.value = 'AB:CD:EF:12:34:56';
    elements.sensorMac.events.input();
    const failedClear = elements.clearOutput.events.click();
    clearReply({ok: false, json: async () => ({error: 'permission denied'})});
    await failedClear;
    assert.equal(elements.sensorMac.value, 'AB:CD:EF:12:34:56');
    assert(elements.liveError.textContent.includes('permission denied'));
    reply({text: '', running: false, sensors: []});
    await settle();
}

testExamples();
testLivePolling().catch(error => { console.error(error); process.exitCode = 1; });
