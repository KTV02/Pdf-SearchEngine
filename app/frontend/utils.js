function formatTime(iso) {
    if (!iso) return "N/A";
    const date = new Date(iso);
    return date.toLocaleString();
}
