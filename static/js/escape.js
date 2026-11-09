// Shared HTML escaping for any value interpolated into innerHTML / template
// strings. Item names can come from the public shopping page or from
// Open Food Facts / UPCitemdb (editable by anyone), so treat them as untrusted.
(function () {
    function escHtml(value) {
        if (value === null || value === undefined) return '';
        return String(value)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }
    window.escHtml = escHtml;
})();
