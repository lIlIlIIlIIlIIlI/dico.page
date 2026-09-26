const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {webcrypto} = require('node:crypto');

const MiB = 1024 * 1024;

function element() {
    return {
        listeners: {}, classList: {add() {}, remove() {}, toggle() {}},
        addEventListener(type, listener) {this.listeners[type] = listener;},
        append() {}, appendChild() {}, replaceChildren() {}, setAttribute() {},
        scrollIntoView() {}, click() {},
    };
}

function pickerAndForm(prefix) {
    const selectors = new Map();
    for (const name of ['attachments', 'picker', 'dropzone', 'list', 'count', 'status', 'select',
        ...(prefix === 'image' ? ['menu', 'paste', 'open'] : [])]) {
        selectors.set(`[data-${prefix}-${name}]`, element());
    }
    selectors.set('[name="csrf_token"]', {value: 'csrf'});
    const root = selectors.get(`[data-${prefix}-attachments]`);
    root.querySelector = selector => selectors.get(selector);
    const picker = selectors.get(`[data-${prefix}-picker]`);
    picker.files = [];
    Object.defineProperty(picker, 'value', {set(value) {if (value === '') this.files = [];}});
    return {picker, form: {querySelector: selector => selectors.get(selector), addEventListener() {}}};
}

function sandboxFor(requests, pendingAt = '') {
    let pendingSent = false;
    return {
        window: {DicoSha256: {create: () => ({update() {}, digest: () => '0'.repeat(64)})}},
        document: {createElement: element}, crypto: webcrypto, AbortController,
        setTimeout: pendingAt ? callback => setImmediate(callback) : setTimeout, clearTimeout,
        URL: {createObjectURL: () => 'blob:media', revokeObjectURL() {}},
        Event: class {constructor(type) {this.type = type;}},
        fetch: async (url, options = {}) => {
            if (url.includes('/chunks/')) requests.push({url, length: options.body.length});
            if (pendingAt && url.includes(pendingAt) && !pendingSent) {
                pendingSent = true;
                return {ok: true, json: async () => ({success: true, pending: true, retry_after: 1.5})};
            }
            return {ok: true, json: async () => url === '/api/media/status'
                ? {configured: true} : {success: true, uploaded_chunks: []}};
        },
    };
}

function load(sandbox, files) {
    for (const file of files) {
        vm.runInNewContext(fs.readFileSync(`api/static/js/${file}`, 'utf8'), sandbox);
    }
}

function fakeFile(name, type, size) {
    return {name, type, size,
        slice(start, end) {
            assert(start >= 0 && end <= size);
            return {arrayBuffer: async () => new ArrayBuffer(end - start)};
        },
    };
}

function assertChunks(requests, route, expected) {
    assert.equal(requests.length, expected.length);
    requests.forEach(({url, length}, index) => {
        assert(url.includes(`/${route}/`), url);
        assert.equal(new URL(url, 'https://example.test').searchParams.get('chunk_index'), String(expected[index][0]));
        assert.equal(new URL(url, 'https://example.test').searchParams.get('chunk_offset'), String(expected[index][1]));
        assert.equal(length, expected[index][2]);
    });
}

async function imageOrVideo(name, type, size, route, expected, pendingAt = '') {
    const requests = [];
    const sandbox = sandboxFor(requests, pendingAt);
    load(sandbox, ['media-upload-queue.js', 'image-attachments.js']);
    const {picker, form} = pickerAndForm('image');
    const uploader = sandbox.window.DicoImageAttachments.mount(form,
        {kind: 'posts', ensureDraft: async () => 35});
    picker.files = [fakeFile(name, type, size)];
    picker.listeners.change();
    await uploader.ready();
    assert.equal(uploader.hasFiles(), true);
    assertChunks(requests, route, expected);
}

async function attachedFile() {
    const requests = [];
    const sandbox = sandboxFor(requests, 'chunk_index=0&chunk_offset=' + 48 * MiB);
    load(sandbox, ['media-upload-queue.js', 'file-attachments.js']);
    const {picker, form} = pickerAndForm('file');
    const uploader = sandbox.window.DicoFileAttachments.mount(form);
    picker.files = [fakeFile('sample.zip', 'application/zip', 51 * MiB)];
    picker.listeners.change();
    await uploader.uploadAll('posts', 35, 'csrf');
    const expected = Array.from({length: 12}, (_, index) => [0, index * 4 * MiB, 4 * MiB]);
    expected.push([0, 48 * MiB, 2 * MiB], [0, 48 * MiB, 2 * MiB], [1, 0, MiB]);
    assertChunks(requests, 'files', expected);
}

(async () => {
    await imageOrVideo('sample.png', 'image/png', 6 * MiB, 'images',
        [[0, 0, 4 * MiB], [0, 4 * MiB, MiB], [1, 0, MiB]]);
    const videoParts = Array.from({length: 12}, (_, index) => [0, index * 4 * MiB, 4 * MiB]);
    videoParts.push([0, 48 * MiB, 2 * MiB], [1, 0, MiB]);
    videoParts.splice(13, 0, [0, 48 * MiB, 2 * MiB]);
    await imageOrVideo('sample.mp4', 'video/mp4', 51 * MiB, 'videos', videoParts,
        'chunk_index=0&chunk_offset=' + 48 * MiB);
    await attachedFile();
    console.log('Photo, video and file chunks stop at logical boundaries and retry pending GitHub blobs.');
})().catch(error => {console.error(error); process.exitCode = 1;});
