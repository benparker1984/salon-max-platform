if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/salon/service-worker.js').catch(() => {});
}
