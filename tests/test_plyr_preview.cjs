const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const listeners = {};
const pageListeners = {};
let page = {videos: []};
let observer;
let streamObserver;
const created = [];

class Observer {
    constructor(callback) { this.callback = callback; observer = this; }
    disconnect() { this.target = null; }
    observe(target) { this.target = target; }
}

class Plyr {
    constructor(video) {
        this.video = video;
        this.elements = {container: {isConnected: true}};
        created.push(this);
    }
    destroy() { this.elements.container.isConnected = false; }
}

class StreamObserver {
    constructor(callback) {this.callback = callback; streamObserver = this;}
    observe(target) {this.target = target;}
    unobserve(target) {this.unobserved = target;}
    disconnect() {this.target = null;}
}

const sandbox = {
    window: {Plyr, IntersectionObserver: StreamObserver, addEventListener: (name, fn) => {listeners[name] = fn;}},
    document: {
        addEventListener: (name, fn) => {pageListeners[name] = fn;},
        getElementById: () => page,
        querySelectorAll: () => page.videos,
    },
    MutationObserver: Observer,
    requestAnimationFrame: callback => callback(),
};

vm.runInNewContext(fs.readFileSync('api/static/js/plyr-media.js', 'utf8'), sandbox);
pageListeners.DOMContentLoaded();
assert.equal(observer.target, page);

listeners['app:navigation-start']();
page = {videos: []};
listeners['app:navigation-end']();
assert.equal(observer.target, page);

page.videos.push({dataset: {}});
observer.callback();
assert.equal(created.length, 1);
assert.equal(page.videos[0].dataset.dicoPlyrReady, 'true');

page.videos.push({dataset: {}});
sandbox.window.DicoPlyr.apply();
assert.equal(created.length, 2);
assert.equal(page.videos[1].dataset.dicoPlyrReady, 'true');

const video = {dataset: {}, paused: true, preload: 'metadata', load() {this.loads = (this.loads || 0) + 1;}};
page.videos.push(video);
sandbox.window.DicoPlyr.apply();
assert.equal(streamObserver.target, video);
streamObserver.callback([{isIntersecting: true, target: video}]);
assert.equal(video.preload, 'auto');
assert.equal(video.loads, 1);
