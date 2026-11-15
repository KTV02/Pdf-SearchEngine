const AdminManager = {
    data() {
        return {
            currentUser: {
                type: '',
                id: '',
                login: '',
                role: '',
                is_admin: false,
                available_indices: [],
                readable_indices: [],
                writable_indices: []
            },
            adminConfig: null,
            storageConfig: null,
            adminLoading: false,
            adminSaving: false,
            storageLoading: false,
            storageSaving: false,
            backupStarting: false,
            adminNewUserLogin: '',
            adminNewIndexName: ''
        };
    },
    computed: {
        adminUserEntries() {
            const users = this.adminConfig?.users || {};
            return Object.keys(users)
                .sort()
                .map(login => ({
                    login,
                    permissions: users[login]
                }));
        }
    },
    methods: {
        normalizeLogin(login) {
            return (login || '').trim().toLowerCase();
        },
        normalizeIndexName(indexName) {
            return (indexName || '').trim();
        },
        fetchCurrentUser() {
            return api.get('/auth/me')
                .then(res => {
                    this.currentUser = res.data;
                    const fetches = [
                        this.fetchCloudProviders ? this.fetchCloudProviders() : Promise.resolve()
                    ];
                    if (this.currentUser.is_admin) {
                        fetches.push(
                            this.fetchAdminAccessConfig(),
                            this.fetchStorageConfig()
                        );
                    }
                    return Promise.all(fetches);
                })
                .catch(err => {
                    console.error("Failed to fetch current user:", err);
                });
        },
        fetchAdminAccessConfig() {
            if (!this.currentUser.is_admin) return Promise.resolve();
            this.adminLoading = true;
            return api.get('/admin/access-config')
                .then(res => {
                    this.adminConfig = res.data.config || { indices: [], users: {} };
                })
                .catch(err => {
                    console.error("Failed to load access configuration:", err);
                    alert(err.response?.data?.error || "Failed to load access configuration.");
                })
                .finally(() => {
                    this.adminLoading = false;
                });
        },
        fetchStorageConfig() {
            if (!this.currentUser.is_admin) return Promise.resolve();
            this.storageLoading = true;
            return api.get('/admin/storage-config')
                .then(res => {
                    this.storageConfig = res.data;
                })
                .catch(err => {
                    console.error("Failed to load storage configuration:", err);
                    alert(err.response?.data?.error || "Failed to load storage configuration.");
                })
                .finally(() => {
                    this.storageLoading = false;
                });
        },
        ensureStorageConfig() {
            if (!this.storageConfig) {
                this.storageConfig = {
                    storage: {},
                    backup: {
                        enabled: false,
                        target_folder: '',
                        interval_hours: 24,
                        retention_count: 7,
                        include_deleted: true,
                        include_configs: true
                    }
                };
            }
            if (!this.storageConfig.backup) {
                this.$set(this.storageConfig, 'backup', {});
            }
            if (!this.storageConfig.storage) {
                this.$set(this.storageConfig, 'storage', {});
            }
        },
        saveStorageConfig() {
            this.ensureStorageConfig();
            this.storageSaving = true;
            api.put('/admin/storage-config', { backup: this.storageConfig.backup })
                .then(res => {
                    this.storageConfig = {
                        storage: res.data.storage,
                        backup: res.data.backup
                    };
                    alert(res.data.message || "Storage backup configuration saved.");
                })
                .catch(err => {
                    alert(err.response?.data?.error || "Failed to save storage backup configuration.");
                })
                .finally(() => {
                    this.storageSaving = false;
                });
        },
        runBackupNow() {
            this.backupStarting = true;
            api.post('/admin/backup/run')
                .then(res => {
                    alert(res.data.message || "Backup task queued.");
                    setTimeout(() => this.fetchStorageConfig(), 1000);
                })
                .catch(err => {
                    alert(err.response?.data?.error || "Failed to start backup.");
                })
                .finally(() => {
                    this.backupStarting = false;
                });
        },
        ensureAdminConfig() {
            if (!this.adminConfig) {
                this.adminConfig = { indices: [], users: {} };
            }
            if (!this.adminConfig.users) {
                this.$set(this.adminConfig, 'users', {});
            }
            if (!Array.isArray(this.adminConfig.indices)) {
                this.$set(this.adminConfig, 'indices', []);
            }
        },
        addAccessUser() {
            this.ensureAdminConfig();
            const login = this.normalizeLogin(this.adminNewUserLogin);
            if (!login) {
                alert("Enter a Tailscale email.");
                return;
            }
            if (!this.adminConfig.users[login]) {
                this.$set(this.adminConfig.users, login, {
                    admin: false,
                    read: [],
                    write: []
                });
            }
            this.adminNewUserLogin = '';
        },
        removeAccessUser(login) {
            if (!confirm(`Remove access entry for ${login}?`)) return;
            this.$delete(this.adminConfig.users, login);
        },
        userHasIndexScope(login, scope, indexName) {
            const permissions = this.adminConfig?.users?.[login] || {};
            return Array.isArray(permissions[scope]) && permissions[scope].includes(indexName);
        },
        setPermissionList(login, scope, indexName, enabled) {
            const permissions = this.adminConfig.users[login];
            const current = Array.isArray(permissions[scope]) ? permissions[scope] : [];
            const next = enabled
                ? Array.from(new Set([...current, indexName]))
                : current.filter(item => item !== indexName);
            this.$set(permissions, scope, next);
        },
        setUserIndexScope(login, scope, indexName, enabled) {
            this.ensureAdminConfig();
            if (!this.adminConfig.users[login]) return;

            this.setPermissionList(login, scope, indexName, enabled);
            if (scope === 'write' && enabled) {
                this.setPermissionList(login, 'read', indexName, true);
            }
            if (scope === 'read' && !enabled) {
                this.setPermissionList(login, 'write', indexName, false);
            }
        },
        saveAdminAccessConfig() {
            this.ensureAdminConfig();
            this.adminSaving = true;
            const payload = JSON.parse(JSON.stringify(this.adminConfig));
            api.put('/admin/access-config', { config: payload })
                .then(res => {
                    this.adminConfig = res.data.config;
                    alert(res.data.message || "Access configuration saved.");
                    this.fetchIndices();
                })
                .catch(err => {
                    alert(err.response?.data?.error || "Failed to save access configuration.");
                })
                .finally(() => {
                    this.adminSaving = false;
                });
        },
        createAdminIndex() {
            const indexName = this.normalizeIndexName(this.adminNewIndexName);
            if (!indexName) {
                alert("Enter an index name.");
                return;
            }

            this.adminSaving = true;
            api.post('/admin/indices', { index_name: indexName })
                .then(res => {
                    if (this.currentUser.is_admin && res.data.config) {
                        this.adminConfig = res.data.config;
                    }
                    this.adminNewIndexName = '';
                    alert(res.data.message || "Index created.");
                    return this.fetchIndices().then(() => {
                        if (this.indices.includes(indexName)) {
                            this.selectedIndex = indexName;
                        }
                    });
                })
                .catch(err => {
                    alert(err.response?.data?.error || "Failed to create index.");
                })
                .finally(() => {
                    this.adminSaving = false;
                });
        }
    }
};
