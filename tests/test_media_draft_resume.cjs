const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const values = new Map([
    ['dico-media-draft:posts', '17'],
    ['dico-media-uploads:posts:17', '[{"id":"media-id"}]'],
]);
const listeners = new Map();
const window = {
    sessionStorage: {
        getItem(key) {return values.get(key) ?? null;},
        setItem(key, value) {values.set(key, String(value));},
        removeItem(key) {values.delete(key);},
    },
    addEventListener(name, callback) {listeners.set(name, callback);},
    removeEventListener(name) {listeners.delete(name);},
};
const input = {value: ''};
const form = {querySelector(selector) {
    if (selector === '[name="media_draft_id"]') return input;
    return {value: 'csrf'};
}};
const requests = [];
const sandbox = {
    window,
    fetch: async (url, options) => {
        requests.push([url, options.method]);
        return {ok: true, json: async () => ({success: true, id: 17})};
    },
};
vm.runInNewContext(fs.readFileSync('api/static/js/media-drafts.js', 'utf8'), sandbox);

(async () => {
    const draft = sandbox.window.DicoMediaDraft.mount(form, 'posts');
    assert.equal(input.value, '17');
    assert.equal(await draft.ensureDraft(), 17);
    assert.deepEqual(requests, [['/api/media/drafts/posts/17/resume', 'POST']]);
    let stopCount = 0;
    draft.setStopUploads(() => {stopCount++;});
    listeners.get('pagehide')({persisted: false});
    assert.equal(stopCount, 1);
    assert.equal(values.get('dico-media-draft:posts'), '17');
    assert.equal(values.get('dico-media-uploads:posts:17'), '[{"id":"media-id"}]');
    assert.equal(requests.length, 1, 'pagehide preserves the server draft for a reload');
    draft.finish();
    assert.equal(values.has('dico-media-draft:posts'), false);
    assert.equal(values.has('dico-media-uploads:posts:17'), false);
    console.log('Draft IDs and upload manifests survive reload and clear after publishing.');
})().catch(error => {
    console.error(error);
    process.exitCode = 1;
});
