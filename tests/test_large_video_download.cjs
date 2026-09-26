const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const size = 101 * 1024 * 1024;
let click;
let written = 0;
let closed = false;
let headCalled = false;
const ranges = [];
const link = {
    href: '/media/posts/1/clip?download=1', download: 'clip.mp4', textContent: '다운로드', dataset: {dicoSize: String(size)},
    getAttribute: () => null, setAttribute() {}, removeAttribute() {},
};
const sandbox = {
    document: {addEventListener: (name, callback) => {click = callback;}},
    window: {
        showSaveFilePicker: async () => {assert.equal(headCalled, false); return {createWritable: async () => ({
            write: async part => {written += part.byteLength;},
            close: async () => {closed = true;},
            abort: async () => {throw new Error('should not abort');},
        })};},
        alert: message => {throw new Error(message);},
    },
    fetch: async (_, options) => {
        if (options.method === 'HEAD') {headCalled = true; return {ok: true, headers: {get: () => String(size)}};}
        const match = /^bytes=(\d+)-(\d+)$/.exec(options.headers.Range);
        assert(match);
        const start = Number(match[1]);
        const end = Number(match[2]);
        ranges.push([start, end]);
        return {status: 206, arrayBuffer: async () => ({byteLength: end - start + 1})};
    },
    Blob: class {constructor() {throw new Error('whole-file Blob should not be created');}},
};

vm.runInNewContext(fs.readFileSync('api/static/js/download-attachments.js', 'utf8'), sandbox);
click({target: {closest: () => link}, defaultPrevented: false, button: 0,
    preventDefault() {}, metaKey: false, ctrlKey: false, shiftKey: false, altKey: false}).then(() => {
    assert.equal(written, size);
    assert.equal(ranges.length, 11);
    assert.equal(closed, true);
}).catch(error => {console.error(error); process.exitCode = 1;});
