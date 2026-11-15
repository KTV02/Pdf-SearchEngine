new Vue({
    el: '#app',
    mixins: [TaskManager, EmailManager, UploadManager, SearchManager, AdminManager, CloudManager],
    data: {
        activeTab: 'search',
        indices: [],
        selectedIndex: '',
        progressMessage: '',
        progressPercentage: 0,
        missingFiles: [],
        loading: false
    },
    computed: {
        isEmailFormValid() {
            const f = this.emailForm;
            return f.email && f.password && f.imap_server && f.imap_port;
        }
    },
    watch: {
        selectedIndex(newIndex) {
            if (newIndex) {
                this.startTaskPolling();
                this.fetchEmailTasks();
            } else {
                this.stopTaskPolling();
                this.currentTaskStatus = null;
            }
        },

        loading(val) {
            console.log("🔁 Loading changed:", val);
        }
    },
    methods: {
        fetchIndices() {
            return api.get('/indices')
                .then(res => {
                    this.indices = res.data.available_indices;
                    this.indexConfigs = res.data.index_configs || {};
                    if (res.data.current_user) {
                        this.currentUser = res.data.current_user;
                    }
                    if (!this.indices.includes(this.selectedIndex)) {
                        this.selectedIndex = this.indices[0] || '';
                    }
                    this.applySelectedIndexConfig();
                })
                .catch(err => {
                    console.error("Failed to fetch indices:", err);
                    alert("Unable to retrieve available indices.");
                });
        },
        processTmp() {
            if (!this.selectedIndex) return alert("Select an index first.");
            this.loading = true;
            api.post('/process-tmp', { index_name: this.selectedIndex })
                .then(res => {
                    if (res.data.task_id) this.startTaskPolling();
                    alert(res.data.message || "TMP processing started.");
                })
                .catch(err => {
                    console.error("TMP process error:", err);
                    alert("TMP processing failed: " + (err.response?.data?.error || err.message));
                })
                .finally(() => {
                    this.loading = false;
                });
        },
        async optimizeIndex() {
            if (!this.selectedIndex) return alert("Select an index first.");
            if (!confirm(`Optimize index "${this.selectedIndex}"?`)) return;

            try {
                const res = await api.post("/maintain-index", {
                    index_name: this.selectedIndex,
                    action: "optimize"
                });
                if (res.data.task_id) this.startTaskPolling();
                alert(res.data.message || "Optimized.");
            } catch (err) {
                alert("Failed to optimize: " + (err.response?.data?.error || err.message));
            } finally {
                this.loading = false;
            }
        },
        async rebuildIndex() {
            if (!this.selectedIndex) return alert("Select an index first.");
            if (!confirm(`⚠️ Rebuild index "${this.selectedIndex}"?\nThis will delete and reindex.`)) return;

            try {
                const res = await api.post("/maintain-index", {
                    index_name: this.selectedIndex,
                    action: "rebuild"
                });
                if (res.data.task_id) this.startTaskPolling();
                alert(res.data.message || "Rebuild started.");
            } catch (err) {
                alert("Failed to rebuild: " + (err.response?.data?.error || err.message));
            } finally {
                this.loading = false;
            }
        },
        verifyIndex() {
            if (!this.selectedIndex) return alert("Please select an index.");
            this.loading = true;

            api.post("/verify-index", { index_name: this.selectedIndex })
                .then(res => {
                    if (res.data.task_id) this.startTaskPolling();
                    alert(res.data.message || "Verification started.");
                })
                .catch(err => {
                    alert("Verification failed: " + (err.response?.data?.error || err.message));
                })
                .finally(() => {
                    this.loading = false;
                });
        }
    },
    mounted() {
        console.log("App mounted ✅");
        this.loading = false;  // Ensure overlay is gone on load
        this.fetchCurrentUser();
        this.fetchIndices();
    },
    beforeDestroy() {
        this.stopTaskPolling();
    }
});
