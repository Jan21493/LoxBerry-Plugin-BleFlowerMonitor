(function () {
    'use strict';

    function inputExamples(prefix, mac, key) {
        const topic = (prefix + '/' + mac + '/' + key).toLowerCase();
        return {
            topic: topic,
            mqtt: topic.replace(/[\/ %]/g, '_'),
            udp: mac.toUpperCase() + '.' + key + '=\\v'
        };
    }

    // Also usable by the regression tests without a browser.
    if (typeof module !== 'undefined') module.exports = {inputExamples: inputExamples};
    if (typeof document === 'undefined') return;

    function init() {
        const messages = document.getElementById('mifloraMessages').dataset;
        const data = document.getElementById('txtLastData');
        const poll = document.querySelector('[name=btnPoll]');
        const clearOutput = document.getElementById('clearOutput');
        const exampleMac = '5C:85:7E:B0:F3:AD';
        let updateSensors = function () {};
        let busy = false;
        let refreshing = false;
        let revision = 0;
        let timer;

        async function request(action, method, parameters) {
            const url = new URL('index.cgi', window.location.href);
            url.search = new URLSearchParams(Object.assign({action: action}, parameters || {})).toString();
            const response = await fetch(url, {method: method || 'GET', cache: 'no-store'});
            const result = await response.json();
            if (!response.ok || result.error) throw new Error(result.error || messages.error);
            return result;
        }

        function showError(error) {
            const box = document.getElementById('liveError');
            if (box) {
                box.textContent = messages.error + ' ' + error.message;
                box.hidden = false;
            }
        }

        function display(area, text, checkbox) {
            if (area.value !== text) {
                const position = area.scrollTop;
                area.value = text;
                area.scrollTop = document.getElementById(checkbox).checked ? area.scrollHeight : position;
            }
        }

        async function update() {
            if (busy || refreshing) return;
            refreshing = true;
            const currentRevision = revision;
            try {
                const status = await request('status');
                if (currentRevision !== revision) return;
                if (data) display(data, status.text, 'dataAutoScroll');
                updateSensors(status.sensors);
                if (clearOutput) clearOutput.disabled = status.running;
                if (poll) {
                    poll.disabled = status.running;
                    if (window.jQuery && window.jQuery(poll).data('mobile-button')) window.jQuery(poll).button('refresh');
                }
                document.getElementById('liveError').hidden = true;
            } catch (error) {
                if (currentRevision === revision) showError(error);
            } finally {
                refreshing = false;
                clearTimeout(timer);
                timer = setTimeout(update, 1000);
            }
        }

        if (data) {
            if (poll) poll.addEventListener('click', async function (event) {
                event.preventDefault();
                if (busy || poll.disabled) return;
                busy = true;
                revision += 1;
                poll.disabled = true;
                if (clearOutput) clearOutput.disabled = true;
                data.value = messages.polling + '\n';
                updateSensors([]);
                try {
                    await request('poll', 'POST');
                } catch (error) {
                    showError(error);
                } finally {
                    busy = false;
                    poll.disabled = false;
                    update();
                }
            });
            if (clearOutput) clearOutput.addEventListener('click', async function () {
                if (busy || clearOutput.disabled) return;
                busy = true;
                revision += 1;
                clearOutput.disabled = true;
                try {
                    await request('clear-output', 'POST');
                    data.value = '';
                    updateSensors([], true);
                    document.getElementById('liveError').hidden = true;
                } catch (error) {
                    showError(error);
                } finally {
                    busy = false;
                    clearOutput.disabled = false;
                    update();
                }
            });
            update();
        }

        const inputs = document.getElementById('loxoneInputs');
        if (inputs) {
            const macInput = document.getElementById('sensorMac');
            const sensorSelect = document.getElementById('sensorSelect');
            const editMac = document.getElementById('editMac');
            const transport = document.getElementById('inputTransport');
            let addresses = [];
            let editing = false;
            updateSensors = function (sensors, reset) {
                if (!sensors) return;
                const next = sensors.length ? sensors : [exampleMac];
                if (!reset && JSON.stringify(next) === JSON.stringify(addresses)) return;
                const previous = addresses;
                addresses = next.slice();
                if (sensorSelect) {
                    sensorSelect.replaceChildren();
                    addresses.forEach(function (address) {
                        const option = document.createElement('option');
                        option.value = address;
                        option.textContent = address + (sensors.length ? '' : ' (' + messages.example + ')');
                        sensorSelect.appendChild(option);
                    });
                }
                if (sensors.length) {
                    editing = false;
                    if (!addresses.includes(macInput.value)) macInput.value = addresses[0];
                    macInput.hidden = true;
                    if (sensorSelect) sensorSelect.hidden = false;
                } else if (reset || (!editing && (!previous.length || !addresses.includes(macInput.value)))) {
                    macInput.value = addresses[0];
                    editing = false;
                    macInput.hidden = true;
                    if (sensorSelect) sensorSelect.hidden = false;
                }
                if (sensorSelect) sensorSelect.value = addresses.includes(macInput.value) ? macInput.value : addresses[0];
                renderInputs();
            };
            function renderInputs() {
                const raw = macInput.value.trim();
                const valid = /^(?:[0-9a-f]{12}|(?:[0-9a-f]{2}:){5}[0-9a-f]{2})$/i.test(raw);
                const mac = raw.replace(/:/g, '').toUpperCase();
                const mqtt = transport.value === 'mqtt';
                document.getElementById('macError').hidden = valid;
                document.getElementById('mqttHelp').hidden = !mqtt;
                document.getElementById('udpHelp').hidden = mqtt;
                document.getElementById('inputColumn').textContent = mqtt ? messages.virtual : messages.udp;
                const subscription = document.getElementById('mqttSubscription');
                if (subscription) subscription.textContent = (inputs.dataset.topic + '/#').toLowerCase();
                inputs.querySelectorAll('.topicColumn').forEach(function (cell) { cell.hidden = !mqtt; });
                inputs.querySelectorAll('[data-key]').forEach(function (row) {
                    const example = inputExamples(inputs.dataset.topic, mac, row.dataset.key);
                    const numeric = !['Name', 'Firmware', 'PollTimeString'].includes(row.dataset.key);
                    row.querySelector('.inputExample').textContent = valid ? (mqtt ? example.mqtt : numeric ? example.udp : '-') : '';
                    row.querySelector('.topicExample').textContent = valid ? example.topic : '';
                });
            }
            macInput.addEventListener('input', renderInputs);
            if (sensorSelect) {
                addresses = Array.from(sensorSelect.options, option => option.value);
                sensorSelect.addEventListener('change', function () {
                    editing = false;
                    macInput.hidden = true;
                    sensorSelect.hidden = false;
                    macInput.value = sensorSelect.value;
                    renderInputs();
                });
            }
            if (editMac) editMac.addEventListener('click', function () {
                editing = true;
                macInput.value = exampleMac;
                macInput.hidden = false;
                if (sensorSelect) sensorSelect.hidden = true;
                macInput.focus();
                macInput.select();
                renderInputs();
            });
            transport.addEventListener('change', renderInputs);
            renderInputs();
        }
    }
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
    else init();
}());
