(() => {
    const selector = 'video[data-dico-player], .dico-media video';
    const players = new Set();
    let scheduled = false;
    const streamObserver = window.IntersectionObserver ? new window.IntersectionObserver(entries => {
        entries.forEach(entry => {
            if (!entry.isIntersecting) return;
            const video = entry.target;
            streamObserver.unobserve(video);
            if (video.paused && video.preload !== 'auto') {
                video.preload = 'auto';
                video.load();
            }
        });
    }, {rootMargin: '350px'}) : null;

    function mount() {
        document.querySelectorAll('video[data-dico-stream]').forEach(video => {
            if (video.dataset.dicoStreamReady) return;
            video.dataset.dicoStreamReady = 'true';
            if (streamObserver) streamObserver.observe(video);
            else { video.preload = 'auto'; video.load?.(); }
        });
        if (!window.Plyr) return;
        for (const player of players) {
            if (player.elements.container.isConnected) continue;
            player.destroy();
            players.delete(player);
        }
        document.querySelectorAll(selector).forEach(video => {
            if (video.dataset.dicoPlyrReady) return;
            video.dataset.dicoPlyrReady = 'true';
            players.add(new window.Plyr(video, {ratio: '16:9'}));
        });
    }

    function scheduleMount() {
        if (scheduled) return;
        scheduled = true;
        requestAnimationFrame(() => {
            scheduled = false;
            mount();
        });
    }

    const observer = new MutationObserver(scheduleMount);
    function observePage() {
        observer.disconnect();
        const page = document.getElementById('app-content');
        if (page) observer.observe(page, {childList: true, subtree: true});
        scheduleMount();
    }

    window.DicoPlyr = {apply: scheduleMount};
    window.addEventListener('app:navigation-start', () => {
        streamObserver?.disconnect();
        for (const player of players) player.destroy();
        players.clear();
    });
    window.addEventListener('app:navigation-end', observePage);
    document.addEventListener('DOMContentLoaded', observePage);
})();
