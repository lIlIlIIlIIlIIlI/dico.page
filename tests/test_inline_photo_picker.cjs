const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {webcrypto} = require('node:crypto');

function element(tagName = 'div') {
    return {
        tagName: tagName.toUpperCase(), children: [], listeners: {},
        classList: {add() {}, remove() {}, toggle() {}},
        addEventListener(type, listener) {this.listeners[type] = listener;},
        append(...children) {this.children.push(...children);},
        appendChild(child) {this.children.push(child);},
        replaceChildren(...children) {this.children = children;},
        setAttribute() {}, scrollIntoView() {}, click() {}, focus() {}, dispatchEvent() {},
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
Object.defineProperty(picker, 'value', {set(value) {if (value === '') this.files = [];}});

const content = element('textarea');
content.value = '앞 문장\n\n뒤 문장';
content.maxLength = 10000;
content.selectionStart = content.selectionEnd = '앞 문장\n\n'.length;
content.setRangeText = function (text, start, end) {
    this.value = this.value.slice(0, start) + text + this.value.slice(end);
    this.selectionStart = this.selectionEnd = start + text.length;
};
let removedOnServer = false;
const sandbox = {
    window: {}, document: {createElement: element}, crypto: webcrypto, AbortController,
    URL: {createObjectURL: () => 'blob:picture', revokeObjectURL() {}},
    fetch: async url => {
        if (url.includes('/remove/')) removedOnServer = true;
        return {ok: true, json: async () => url === '/api/media/status'
            ? {configured: true} : {success: true}};
    },
    Event: class {constructor(type) {this.type = type;}},
};
for (const file of ['media-upload-queue.js', 'image-attachments.js']) {
    vm.runInNewContext(fs.readFileSync('api/static/js/' + file, 'utf8'), sandbox);
}
const uploader = sandbox.window.DicoImageAttachments.mount({
    querySelector: selector => selectors.get(selector), addEventListener() {},
}, {kind: 'posts', ensureDraft: async () => 17, contentInput: content});

picker.files = [{name: 'picture.png', type: 'image/png', size: 4}];
picker.listeners.change();

(async () => {
    await uploader.ready();
    const marker = content.value.match(/!\[picture\.png\]\(dico-image:([0-9a-f-]{36})\)/)?.[0];
    assert(marker);
    assert(content.value.indexOf('앞 문장') < content.value.indexOf(marker));
    assert(content.value.indexOf(marker) < content.value.indexOf('뒤 문장'));
    assert.equal(uploader.mediaForToken(marker).name, 'picture.png');
    assert(uploader.previewText().includes('#[DICO-IMAGE-' + uploader.mediaForToken(marker).id + ']'));
    assert.equal(uploader.mediaForPreviewHeading('[DICO-IMAGE-' + uploader.mediaForToken(marker).id + ']').name, 'picture.png');

    assert.equal(selectors.get('[data-image-list]').children.length, 1);
    let buttons = selectors.get('[data-image-list]').children[0].children.filter(child => child.tagName === 'BUTTON');
    assert.deepEqual(buttons.map(button => button.textContent), ['본문에 삽입', '삭제']);
    content.selectionStart = content.selectionEnd = content.value.length;
    buttons[0].listeners.click();
    assert.equal(content.value.split(marker).length - 1, 1);
    assert(content.value.indexOf('뒤 문장') < content.value.indexOf(marker));

    buttons = selectors.get('[data-image-list]').children[0].children.filter(child => child.tagName === 'BUTTON');
    buttons[1].listeners.click();
    await uploader.ready();
    assert.equal(content.value.includes(marker), false);
    assert.equal(removedOnServer, true);
    console.log('Photo marker inserts, moves, previews, and clears without a duplicate preview button.');
})().catch(error => {console.error(error); process.exitCode = 1;});
