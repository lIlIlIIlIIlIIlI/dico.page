window.DicoMediaDraft = window.DicoMediaDraft || (() => {
    function mount(form, kind) {
        const input = form.querySelector('[name="media_draft_id"]');
        const csrfToken = form.querySelector('[name="csrf_token"]').value;
        const draftKey = 'dico-media-draft:' + kind;
        const uploadKey = draftId => 'dico-media-uploads:' + kind + ':' + draftId;
        function storedId() {
            try {
                const value = Number(window.sessionStorage?.getItem(draftKey));
                return Number.isSafeInteger(value) && value > 0 ? value : null;
            } catch (_) { return null; }
        }
        function saveId(value) {
            try {
                if (value) window.sessionStorage?.setItem(draftKey, String(value));
                else window.sessionStorage?.removeItem(draftKey);
            } catch (_) {}
        }
        let id = storedId();
        if (id) input.value = String(id);
        let reservation = null;
        let verified = false;
        let finished = false;
        let waitForUploads = () => Promise.resolve();
        let stopUploads = () => {};

        function url() {
            return '/api/media/drafts/' + kind + '/' + id + '/cancel';
        }

        function detach() {
            window.removeEventListener('app:navigation-start', navigationCleanup);
            window.removeEventListener('pagehide', preserveForReload);
        }

        async function ensureDraft() {
            if (id && verified) return id;
            if (!reservation) {
                reservation = (async () => {
                    if (id) {
                        const resume = await fetch('/api/media/drafts/' + kind + '/' + id + '/resume', {
                            method: 'POST', credentials: 'same-origin',
                            headers: {'X-CSRF-Token': csrfToken, 'Accept': 'application/json'}
                        });
                        const restored = await resume.json();
                        if (resume.ok && restored.success) {
                            verified = true;
                            input.value = String(id);
                            return id;
                        }
                        complete();
                    }
                    const response = await fetch('/api/media/drafts/' + kind, {
                        method: 'POST', credentials: 'same-origin',
                        headers: {'X-CSRF-Token': csrfToken, 'Accept': 'application/json'}
                    });
                    const data = await response.json();
                    if (!response.ok || !data.success) throw new Error(data.message || '임시 업로드를 시작하지 못했습니다.');
                    id = data.id;
                    verified = true;
                    input.value = String(id);
                    saveId(id);
                    return id;
                })().catch(error => { reservation = null; throw error; });
            }
            return reservation;
        }

        function complete() {
            if (id) {
                try { window.sessionStorage?.removeItem(uploadKey(id)); } catch (_) {}
            }
            id = null;
            verified = false;
            reservation = null;
            input.value = '';
            saveId(null);
        }

        function finish() {
            complete();
            finished = true;
            detach();
        }

        async function cancel(keepMounted = false) {
            if (finished) return;
            if (reservation) await reservation.catch(() => {});
            if (id) {
                const response = await fetch(url(), {
                    method: 'POST', credentials: 'same-origin',
                    headers: {'X-CSRF-Token': csrfToken, 'Accept': 'application/json'}
                });
                const data = await response.json();
                if (!response.ok || !data.success) throw new Error(data.message || '첨부파일을 삭제하지 못했습니다.');
            }
            if (keepMounted) complete();
            else finish();
        }

        function preserveForReload(event) {
            if (finished) return;
            if (event?.persisted) return;
            stopUploads();
            // Preserve the MongoDB draft so the same selected file can resume after reload.
        }

        async function navigationCleanup() {
            if (finished) return;
            try {
                stopUploads();
                await waitForUploads();
                await cancel();
            } catch (_) {
                preserveForReload();
            }
        }

        window.addEventListener('app:navigation-start', navigationCleanup);
        window.addEventListener('pagehide', preserveForReload);
        return {ensureDraft, cancel, complete, finish, id: () => id,
            setWaitForUploads: callback => { waitForUploads = callback; },
            setStopUploads: callback => { stopUploads = callback; }};
    }

    return {mount};
})();
