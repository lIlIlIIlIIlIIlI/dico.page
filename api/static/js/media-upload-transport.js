window.DicoMediaUpload = window.DicoMediaUpload || (() => {
    const MiB = 1024 * 1024;

    // Shorter requests waste less time when a slow connection drops; Vercel caps each body.
    function chunkSize(bytesPerSecond) {
        if (bytesPerSecond && bytesPerSecond < 512 * 1024) return MiB;
        if (bytesPerSecond && bytesPerSecond < 2 * MiB) return 2 * MiB;
        return 4 * MiB;
    }

    async function post(url, body, options, onProgress) {
        if (typeof XMLHttpRequest === 'undefined') return fetch(url, options);
        return new Promise((resolve, reject) => {
            const xhr = new XMLHttpRequest();
            let settled = false;
            const signal = options.signal;
            const abort = () => xhr.abort();
            const cleanup = () => signal?.removeEventListener('abort', abort);
            const fail = message => {
                if (settled) return;
                settled = true;
                cleanup();
                reject(new Error(message));
            };
            xhr.open('POST', url, true);
            xhr.withCredentials = true;
            Object.entries(options.headers || {}).forEach(([name, value]) => xhr.setRequestHeader(name, value));
            xhr.upload.onprogress = event => {
                if (event.lengthComputable) onProgress?.(event.loaded, event.total);
            };
            xhr.onload = () => {
                if (settled) return;
                settled = true;
                cleanup();
                let data = {};
                try { data = JSON.parse(xhr.responseText); } catch (_) {}
                resolve({ok: xhr.status >= 200 && xhr.status < 300, status: xhr.status,
                    headers: {get: name => xhr.getResponseHeader(name)}, json: async () => data});
            };
            xhr.onerror = () => fail('업로드 연결이 끊어졌습니다. 다시 시도합니다.');
            xhr.onabort = () => fail('업로드가 취소되었습니다.');
            if (signal?.aborted) { fail('업로드가 취소되었습니다.'); return; }
            signal?.addEventListener('abort', abort, {once: true});
            xhr.send(body);
        });
    }

    return {chunkSize, post};
})();
