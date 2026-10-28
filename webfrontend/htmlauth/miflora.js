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
            let realSensors = false;
            updateSensors = function (sensors, reset) {
                if (!sensors) return;
                realSensors = sensors.length > 0;
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
                inputs.querySelectorAll('.mqttSubscription').forEach(function (subscription) {
                    subscription.textContent = (inputs.dataset.topic + '/#').toLowerCase();
                });
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
                realSensors = addresses.length > 0 && !(sensorSelect.options[0].dataset || {}).example;
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

            const dialog = document.getElementById('templateDialog');
            const exportButton = document.getElementById('exportTemplate');
            if (dialog && exportButton) initTemplateDialog(dialog, exportButton, function () {
                const list = [];
                function add(value) {
                    const raw = (value || '').trim();
                    if (!/^(?:[0-9a-f]{12}|(?:[0-9a-f]{2}:){5}[0-9a-f]{2})$/i.test(raw)) return;
                    const mac = raw.replace(/:/g, '').toUpperCase().match(/../g).join(':');
                    if (!list.includes(mac)) list.push(mac);
                }
                if (realSensors) addresses.forEach(add);
                if (editing || !list.length) add(macInput.value);
                return list;
            });
        }

        function initTemplateDialog(dialog, exportButton, templateAddresses) {
            const sensorRows = document.getElementById('templateSensors');
            const paramRows = document.getElementById('templateParams');
            const templateError = document.getElementById('templateError');
            const templateStored = document.getElementById('templateStored');

            function textInput(className, value, label, placeholder) {
                const field = document.createElement('input');
                field.type = 'text';
                field.className = className;
                field.value = value || '';
                field.maxLength = className === 'templateUnit' ? 32 : 64;
                field.setAttribute('data-role', 'none');
                field.setAttribute('aria-label', label);
                if (placeholder) field.placeholder = placeholder;
                return field;
            }

            function row(cells) {
                const tr = document.createElement('tr');
                cells.forEach(function (content) {
                    const td = document.createElement('td');
                    if (typeof content === 'string') td.textContent = content;
                    else td.appendChild(content);
                    tr.appendChild(td);
                });
                return tr;
            }

            function renderParams(params) {
                paramRows.replaceChildren();
                params.forEach(function (param) {
                    const tr = row([param.key,
                        textInput('templateLabel', param.label, param.key, param.defaultLabel),
                        textInput('templateUnit', param.unit, param.key, param.defaultUnit)]);
                    tr.dataset.param = param.key;
                    paramRows.appendChild(tr);
                });
            }

            function showTemplateError(error) {
                templateError.textContent = error.message;
                templateError.hidden = false;
            }

            function closeDialog() {
                if (typeof dialog.close === 'function') dialog.close();
                else dialog.removeAttribute('open');
            }

            exportButton.addEventListener('click', async function () {
                templateError.hidden = true;
                templateStored.hidden = true;
                try {
                    const settings = await request('template-settings');
                    sensorRows.replaceChildren();
                    templateAddresses().forEach(function (mac) {
                        const include = document.createElement('input');
                        include.type = 'checkbox';
                        include.checked = true;
                        include.setAttribute('data-role', 'none');
                        const label = document.createElement('label');
                        label.appendChild(include);
                        label.appendChild(document.createTextNode(' ' + mac));
                        const tr = row([label, textInput('templateName', settings.names[mac], mac, messages.namePlaceholder)]);
                        tr.dataset.mac = mac;
                        sensorRows.appendChild(tr);
                    });
                    renderParams(settings.params);
                    if (typeof dialog.showModal === 'function') dialog.showModal();
                    else dialog.setAttribute('open', '');
                } catch (error) {
                    showError(error);
                }
            });

            dialog.querySelectorAll('[data-close]').forEach(function (button) {
                button.addEventListener('click', closeDialog);
            });

            document.getElementById('templateReset').addEventListener('click', async function () {
                templateError.hidden = true;
                try {
                    renderParams((await request('template-reset', 'POST')).params);
                } catch (error) {
                    showTemplateError(error);
                }
            });

            document.getElementById('templateDownload').addEventListener('click', async function () {
                templateError.hidden = true;
                templateStored.hidden = true;
                const body = new URLSearchParams();
                sensorRows.querySelectorAll('tr').forEach(function (tr) {
                    if (!tr.querySelector('input[type=checkbox]').checked) return;
                    body.append('mac', tr.dataset.mac);
                    body.append('name', tr.querySelector('.templateName').value);
                });
                paramRows.querySelectorAll('tr').forEach(function (tr) {
                    body.append('label_' + tr.dataset.param, tr.querySelector('.templateLabel').value);
                    body.append('unit_' + tr.dataset.param, tr.querySelector('.templateUnit').value);
                });
                try {
                    const url = new URL('index.cgi', window.location.href);
                    url.search = 'action=template-export';
                    const response = await fetch(url, {method: 'POST', body: body, cache: 'no-store'});
                    if (!response.ok) {
                        const result = await response.json().catch(function () { return {}; });
                        throw new Error(result.error || messages.error);
                    }
                    const match = /filename="?([^";]+)"?/.exec(response.headers.get('Content-Disposition') || '');
                    const link = document.createElement('a');
                    link.href = URL.createObjectURL(await response.blob());
                    link.download = match ? match[1] : 'VI_BLE Flower Monitor MQTT Inputs.xml';
                    document.body.appendChild(link);
                    link.click();
                    link.remove();
                    setTimeout(function () { URL.revokeObjectURL(link.href); }, 1000);
                    const stored = response.headers.get('X-Template-File');
                    if (stored) {
                        templateStored.textContent = messages.stored + ' ' + stored;
                        templateStored.hidden = false;
                    }
                } catch (error) {
                    showTemplateError(error);
                }
            });
        }
    }
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
    else init();
}());
