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
        setAttribute() {}, scrollIntoView() {}, focus() {}, dispatchEvent() {},
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

function mediaElement() {
    const video = element('video');
    video.canPlayType = () => 'probably';
    video.videoWidth = 640;
    video.videoHeight = 360;
    video.duration = 10;
    video.seeking = false;
    video.removeAttribute = () => {video.src = '';};
    video.load = () => {
        if (!video.src) return;
        queueMicrotask(() => {
            video.listeners.loadedmetadata?.();
            video.listeners.loadeddata?.();
        });
    };
    return video;
}

const canvas = element('canvas');
canvas.getContext = () => ({drawImage() {}});
canvas.toBlob = callback => callback(new Blob(['RIFF\0\0\0\0WEBPpreview'], {type: 'image/webp'}));
const poster = 'data:image/webp;base64,' + Buffer.from('RIFF\0\0\0\0WEBPpreview').toString('base64');
class FileReader {
    readAsDataURL() {this.result = poster; queueMicrotask(() => this.onload());}
}

const completions = [];
const content = element('textarea');
content.value = '본문';
content.maxLength = 10000;
content.selectionStart = content.selectionEnd = content.value.length;
content.setRangeText = function (value, start, end) {
    this.value = this.value.slice(0, start) + value + this.value.slice(end);
    this.selectionStart = this.selectionEnd = start + value.length;
};
const sandbox = {
    window: {FileReader, location: {origin: 'https://dico.page'}}, FileReader,
    document: {createElement: tag => tag === 'video' ? mediaElement() : tag === 'canvas' ? canvas : element(tag)},
    crypto: webcrypto, AbortController, Blob, setTimeout, clearTimeout,
    URL: {createObjectURL: () => 'blob:clip', revokeObjectURL() {}},
    fetch: async (url, options) => {
        if (url.endsWith('/complete')) completions.push(JSON.parse(options.body));
        return {ok: true, json: async () => url === '/api/media/status' ? {configured: true} : {success: true}};
    },
    Event: class {constructor(type) {this.type = type;}},
};
for (const file of ['media-upload-queue.js', 'image-attachments.js']) {
    vm.runInNewContext(fs.readFileSync('api/static/js/' + file, 'utf8'), sandbox);
}
const uploader = sandbox.window.DicoImageAttachments.mount({
    querySelector: selector => selectors.get(selector), addEventListener() {},
}, {kind: 'posts', ensureDraft: async () => 17, contentInput: content});

picker.files = [{name: 'clip.mp4', type: 'video/mp4', size: 1000,
    slice(start, end) {return {size: end - start};}}];
picker.listeners.change();

uploader.ready().then(() => {
    assert.equal(completions.length, 1);
    assert.equal(completions[0].poster, poster);
    const row = selectors.get('[data-image-list]').children[0];
    assert.equal(row.children[0].tagName, 'IMG');
    const marker = content.value.match(/#\[([0-9a-f-]{36})\]/)?.[0];
    assert(marker);
    assert.equal(uploader.mediaForToken(marker).name, 'clip.mp4');
    assert.equal(uploader.mediaForPreviewHeading(marker.slice(1)).name, 'clip.mp4');
    assert.equal(row.children[1].children[1].href, 'https://dico.page/media/posts/17/' + marker.slice(2, -1));
    assert.equal(content.value.includes('#[clip.mp4]'), false);
    console.log('Video preview frame is uploaded with the video and shown in the picker.');
}).catch(error => {console.error(error); process.exitCode = 1;});
