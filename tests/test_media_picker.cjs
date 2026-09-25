const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {webcrypto} = require('node:crypto');

function element() {
    return {
        listeners: {},
        classList: {add() {}, remove() {}, toggle() {}},
        addEventListener(type, listener) { this.listeners[type] = listener; },
        append() {}, appendChild() {}, replaceChildren() {}, setAttribute() {},
        scrollIntoView() {}, click() {},
    };
}

const selectors = new Map();
for (const selector of [
    '[data-image-attachments]', '[data-image-picker]', '[data-image-dropzone]',
    '[data-image-list]', '[data-image-count]', '[data-image-status]', '[data-image-menu]',
    '[data-image-select]', '[data-image-paste]', '[data-image-open]', '[name="csrf_token"]',
]) selectors.set(selector, element());

selectors.get('[data-image-attachments]').querySelector = selector => selectors.get(selector);
selectors.get('[name="csrf_token"]').value = 'csrf';
const picker = selectors.get('[data-image-picker]');
picker.files = [];
Object.defineProperty(picker, 'value', {
    set(value) { if (value === '') this.files = []; },
});

const requests = [];
const sandbox = {
    window: {}, document: {createElement: element}, crypto: webcrypto,
    URL: {createObjectURL: () => 'blob:preview', revokeObjectURL() {}},
    fetch: async url => {
        requests.push(url);
        return {ok: true, json: async () => url === '/api/media/status'
            ? {configured: true} : {success: true}};
    },
    Event: class {constructor(type) { this.type = type; }},
};
vm.runInNewContext(fs.readFileSync('api/static/js/image-attachments.js', 'utf8'), sandbox);

const uploader = sandbox.window.DicoImageAttachments.mount({
    querySelector: selector => selectors.get(selector), addEventListener() {},
}, {kind: 'posts', ensureDraft: async () => 17});

picker.files = [{name: 'picked.png', type: 'image/png', size: 4}];
picker.listeners.change();
uploader.ready().then(() => {
    assert.equal(uploader.hasFiles(), true);
    assert(requests.some(url => /^\/api\/media\/posts\/17\/images\/.*\?name=picked\.png$/.test(url)));
    console.log('File picker keeps its selection until the upload starts.');
}).catch(error => {
    console.error(error);
    process.exitCode = 1;
});
