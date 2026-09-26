const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {webcrypto} = require('node:crypto');

const MiB = 1024 * 1024;
let clock = 0;
const sent = [];
const visible = [];

function element() {
    return {
        classList: {add() {}, remove() {}, toggle() {}}, listeners: {},
        addEventListener(name, handler) {this.listeners[name] = handler;},
        append(...children) {children.forEach(child => {if (child.textContent) visible.push(child.textContent);});},
        appendChild() {}, replaceChildren() {}, setAttribute() {}, click() {},
    };
}
const selectors = new Map();
for (const name of ['attachments', 'picker', 'dropzone', 'list', 'count', 'status', 'select']) {
    selectors.set(`[data-file-${name}]`, element());
}
const root = selectors.get('[data-file-attachments]');
root.querySelector = selector => selectors.get(selector);
const picker = selectors.get('[data-file-picker]');
Object.defineProperty(picker, 'value', {set() {this.files = [];}});
const form = {querySelector: selector => selectors.get(selector), addEventListener() {}};

class FakeXHR {
    constructor() {this.upload = {};}
    open(method, url) {this.url = url;}
    setRequestHeader() {}
    getResponseHeader() {return null;}
    send(body) {
        if (this.url.includes('/chunks/')) {
            const start = Number(this.url.match(/\/chunks\/(\d+)/)[1]);
            sent.push({start, size: body.length, wireSize: Number(new URL(this.url, 'https://dico.page').searchParams.get('wire_size'))});
            clock += 5000;
            this.upload.onprogress({lengthComputable: true, loaded: body.length / 2, total: body.length});
            clock += 5000;
            this.upload.onprogress({lengthComputable: true, loaded: body.length, total: body.length});
            this.responseText = JSON.stringify({success: true, uploaded_bytes: start + body.length});
        } else {
            this.responseText = JSON.stringify({success: true});
        }
        this.status = 200;
        this.onload();
    }
    abort() {this.onabort();}
}

const sandbox = {
    window: {DicoSha256: {create: () => ({update() {}, digest: () => '0'.repeat(64)})}},
    document: {createElement: element}, crypto: webcrypto, XMLHttpRequest: FakeXHR,
    Date: class extends Date {static now() {return clock;}}, URL,
    setTimeout, clearTimeout,
    fetch: async url => ({ok: true, json: async () => url.endsWith('/status')
        ? {success: true, protocol_version: 2, uploaded_bytes: 0}
        : {configured: true}}),
};
for (const name of ['media-upload-queue.js', 'media-upload-transport.js', 'file-attachments.js']) {
    vm.runInNewContext(fs.readFileSync('api/static/js/' + name, 'utf8'), sandbox);
}

async function main() {
    const uploader = sandbox.window.DicoFileAttachments.mount(form);
    picker.files = [{name: 'sample.txt', size: 6 * MiB, type: 'text/plain',
        slice(start, end) {return {arrayBuffer: async () => new ArrayBuffer(end - start)};}}];
    picker.listeners.change();
    await uploader.uploadAll('posts', 9, 'csrf');
    assert.deepEqual(sent.map(item => item.size), [4 * MiB, MiB, MiB]);
    assert.deepEqual(sent.map(item => item.start), [0, 4 * MiB, 5 * MiB]);
    assert(visible.some(value => /전송 중 · 33\.33%/.test(value)), 'Upload progress should update within a request');
    assert(visible.some(value => /GitHub 저장 중 · 66\.67%/.test(value)));
    console.log('Direct upload reports live percentages and shrinks requests on a slow connection.');
}
main().catch(error => {console.error(error); process.exitCode = 1;});
