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
        const refresh = document.querySelector('[name=btnRefresh]');
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
                if (poll) {
                    poll.disabled = status.running;
                    if (window.jQuery && window.jQuery(poll).data('mobile-button')) window.jQuery(poll).button('refresh');
                }
                document.getElementById('liveError').hidden = true;
            } catch (error) {
                showError(error);
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
                data.value = messages.polling + '\n';
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
            if (refresh) refresh.addEventListener('click', function (event) {
                event.preventDefault();
                update();
            });
            update();
        }

        const inputs = document.getElementById('loxoneInputs');
        if (inputs) {
            const macInput = document.getElementById('sensorMac');
            const transport = document.getElementById('inputTransport');
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
            transport.addEventListener('change', renderInputs);
            renderInputs();
        }
    }
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
    else init();
}());
