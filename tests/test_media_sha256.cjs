const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const sandbox = {window: {}};
vm.runInNewContext(fs.readFileSync('api/static/js/media-sha256.js', 'utf8'), sandbox);

const vectors = [
    ['', 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'],
    ['abc', 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad'],
    ['a'.repeat(1000), '41edece42d63e8d9bf515a9ba6932e1c20cbc9f5a5d134645adb5db1b9737ea3'],
];
for (const [value, expected] of vectors) {
    const bytes = new TextEncoder().encode(value);
    const digest = sandbox.window.DicoSha256.create();
    for (let offset = 0; offset < bytes.length; offset += 37) {
        digest.update(bytes.subarray(offset, offset + 37));
    }
    assert.equal(digest.digest(), expected);
}
console.log('Incremental SHA-256 matches standard vectors across uneven input slices.');
