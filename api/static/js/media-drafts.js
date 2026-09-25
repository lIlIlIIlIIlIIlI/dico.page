window.DicoMediaDraft = window.DicoMediaDraft || (() => {
    function mount(form, kind) {
        const input = form.querySelector('[name="media_draft_id"]');
        const csrfToken = form.querySelector('[name="csrf_token"]').value;
        let id = null;
        let reservation = null;
        let finished = false;
        let waitForUploads = () => Promise.resolve();

        function url() {
            return '/api/media/drafts/' + kind + '/' + id + '/cancel';
        }

        function detach() {
            window.removeEventListener('app:navigation-start', navigationCleanup);
            window.removeEventListener('pagehide', beacon);
        }

        async function ensureDraft() {
            if (id) return id;
            if (!reservation) {
                reservation = fetch('/api/media/drafts/' + kind, {
                    method: 'POST', credentials: 'same-origin',
                    headers: {'X-CSRF-Token': csrfToken, 'Accept': 'application/json'}
                }).then(async response => {
                    const data = await response.json();
                    if (!response.ok || !data.success) throw new Error(data.message || '임시 업로드를 시작하지 못했습니다.');
                    id = data.id;
                    input.value = String(id);
                    return id;
                }).catch(error => { reservation = null; throw error; });
            }
            return reservation;
        }

        function complete() {
            id = null;
            reservation = null;
            input.value = '';
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

        function beacon() {
            if (finished || !id) return;
            const data = new FormData();
            data.append('csrf_token', csrfToken);
            navigator.sendBeacon?.(url(), data);
            finish();
        }

        async function navigationCleanup() {
            if (finished) return;
            try {
                await waitForUploads();
                await cancel();
            } catch (_) {
                beacon();
            }
        }

        window.addEventListener('app:navigation-start', navigationCleanup);
        window.addEventListener('pagehide', beacon);
        return {ensureDraft, cancel, complete, finish, id: () => id, setWaitForUploads: callback => { waitForUploads = callback; }};
    }

    return {mount};
})();
