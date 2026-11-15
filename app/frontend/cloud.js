const CloudManager = {
    data() {
        return {
            indexConfigs: {},
            selectedIndexConfig: null,
            indexConfigForm: {
                mode: 'standalone',
                cloud: {
                    provider: 'mounted_folder',
                    root_path: '',
                    connection_id: '',
                    remote_path: '',
                    managed_root_path: '',
                    display_root_name: '',
                    preview_folder: '',
                    sync_enabled: true,
                    sync_interval_seconds: 300,
                    delete_standalone_duplicates: true
                }
            },
            indexConfigLoading: false,
            indexConfigSaving: false,
            cloudProviders: [],
            cloudConnections: [],
            cloudProviderSettings: {
                onedrive: {
                    client_id: '',
                    tenant: 'common',
                    scopes: 'offline_access User.Read Files.ReadWrite',
                    request_timeout_seconds: 60
                },
                google_drive: {
                    client_id: '',
                    client_secret: '',
                    client_secret_saved: false,
                    redirect_uri: '',
                    scopes: 'https://www.googleapis.com/auth/drive https://www.googleapis.com/auth/userinfo.email https://www.googleapis.com/auth/userinfo.profile',
                    request_timeout_seconds: 60
                },
                dropbox: {
                    app_key: '',
                    app_secret: '',
                    app_secret_saved: false,
                    redirect_uri: '',
                    scopes: 'account_info.read files.metadata.read files.content.read files.content.write',
                    request_timeout_seconds: 60
                }
            },
            canManageProviderSettings: false,
            cloudHelpVisible: {},
            cloudProviderLoading: false,
            cloudProviderSettingsSaving: false,
            cloudConnectionStarting: false,
            cloudLoginSession: null,
            cloudLoginPollTimer: null,
            cloudSyncStarting: false,
            folderPath: '',
            folderTree: null,
            folderLoading: false,
            folderPreview: null
        };
    },
    computed: {
        currentIndexMode() {
            return this.selectedIndexConfig?.mode || 'standalone';
        },
        uploadsEnabled() {
            return this.selectedIndexConfig?.capabilities?.uploads_enabled !== false;
        },
        folderViewEnabled() {
            return this.selectedIndexConfig?.capabilities?.folder_view_enabled === true;
        },
        cloudSyncEnabled() {
            return this.selectedIndexConfig?.capabilities?.cloud_sync_enabled === true;
        },
        indexConfigDirty() {
            if (!this.selectedIndexConfig || !this.indexConfigForm) return false;
            return this.indexConfigComparable(this.indexConfigForm) !== this.indexConfigComparable(this.selectedIndexConfig);
        },
        selectedIndexWritable() {
            if (!this.selectedIndex) return false;
            if (this.currentUser?.is_admin) return true;
            const writable = this.currentUser?.writable_indices || this.currentUser?.write || [];
            return Array.isArray(writable) && writable.includes(this.selectedIndex);
        },
        selectedProviderRequiresAdmin() {
            const mode = this.indexConfigForm?.mode || 'standalone';
            const provider = this.indexConfigForm?.cloud?.provider || 'mounted_folder';
            return !this.currentUser?.is_admin
                && (mode === 'cloud' || mode === 'hybrid')
                && provider === 'mounted_folder';
        },
        availableCloudProviders() {
            const providers = this.cloudProviders.length ? this.cloudProviders : [
                {
                    id: 'mounted_folder',
                    label: 'Mounted or synced folder',
                    requires_login: false,
                    configured: true
                },
                {
                    id: 'onedrive',
                    label: 'OneDrive',
                    requires_login: true,
                    configured: false,
                    missing_configuration: 'OneDrive provider registration is not saved yet'
                },
                {
                    id: 'google_drive',
                    label: 'Google Drive',
                    requires_login: true,
                    configured: false,
                    missing_configuration: 'Google Drive provider registration is not saved yet'
                },
                {
                    id: 'dropbox',
                    label: 'Dropbox',
                    requires_login: true,
                    configured: false,
                    missing_configuration: 'Dropbox provider registration is not saved yet'
                }
            ];
            if (this.currentUser?.is_admin) return providers;
            const selectedProvider = this.indexConfigForm?.cloud?.provider || '';
            return providers.filter(provider => provider.id !== 'mounted_folder' || provider.id === selectedProvider);
        },
        currentIndexModeLabel() {
            const labels = {
                standalone: 'Standalone',
                cloud: 'Cloud',
                hybrid: 'Hybrid'
            };
            return labels[this.currentIndexMode] || 'Standalone';
        }
    },
    watch: {
        selectedIndex(newIndex) {
            this.folderPath = '';
            this.folderTree = null;
            this.folderPreview = null;
            if (newIndex) {
                this.applySelectedIndexConfig();
                this.fetchIndexConfig();
                if (this.activeTab === 'folder') {
                    this.fetchFolder('');
                }
            }
        },
        activeTab(newTab) {
            if (newTab === 'folder' && this.selectedIndex) {
                this.fetchFolder(this.folderPath || '');
            }
            if (newTab === 'admin') {
                this.fetchCloudProviders();
            }
        }
    },
    beforeDestroy() {
        this.clearCloudLoginPolling();
    },
    methods: {
        defaultIndexConfigForm() {
            return {
                mode: 'standalone',
                cloud: {
                    provider: 'mounted_folder',
                    root_path: '',
                    connection_id: '',
                    remote_path: '',
                    managed_root_path: '',
                    display_root_name: '',
                    preview_folder: '',
                    sync_enabled: true,
                    sync_interval_seconds: 300,
                    delete_standalone_duplicates: true
                }
            };
        },
        clone(value) {
            return JSON.parse(JSON.stringify(value || {}));
        },
        indexConfigComparable(config) {
            const cloud = config?.cloud || {};
            return JSON.stringify({
                mode: config?.mode || 'standalone',
                cloud: {
                    provider: cloud.provider || 'mounted_folder',
                    root_path: cloud.root_path || '',
                    connection_id: cloud.connection_id || '',
                    remote_path: cloud.remote_path || '',
                    managed_root_path: cloud.managed_root_path || '',
                    display_root_name: cloud.display_root_name || '',
                    preview_folder: cloud.preview_folder || '',
                    sync_enabled: cloud.sync_enabled !== false,
                    sync_interval_seconds: Number(cloud.sync_interval_seconds || 300),
                    delete_standalone_duplicates: cloud.delete_standalone_duplicates !== false
                }
            });
        },
        defaultCloudProviderSettings() {
            return {
                onedrive: {
                    client_id: '',
                    tenant: 'common',
                    scopes: 'offline_access User.Read Files.ReadWrite',
                    request_timeout_seconds: 60
                },
                google_drive: {
                    client_id: '',
                    client_secret: '',
                    client_secret_saved: false,
                    redirect_uri: '',
                    scopes: 'https://www.googleapis.com/auth/drive https://www.googleapis.com/auth/userinfo.email https://www.googleapis.com/auth/userinfo.profile',
                    request_timeout_seconds: 60
                },
                dropbox: {
                    app_key: '',
                    app_secret: '',
                    app_secret_saved: false,
                    redirect_uri: '',
                    scopes: 'account_info.read files.metadata.read files.content.read files.content.write',
                    request_timeout_seconds: 60
                }
            };
        },
        ensureCloudProviderSettings() {
            if (!this.cloudProviderSettings) {
                this.cloudProviderSettings = this.defaultCloudProviderSettings();
            }
            const defaults = this.defaultCloudProviderSettings();
            Object.keys(defaults).forEach(provider => {
                if (!this.cloudProviderSettings[provider]) {
                    this.$set(this.cloudProviderSettings, provider, defaults[provider]);
                }
            });
            const onedrive = this.cloudProviderSettings.onedrive;
            if (onedrive.client_id === undefined) this.$set(onedrive, 'client_id', '');
            if (onedrive.tenant === undefined) this.$set(onedrive, 'tenant', 'common');
            if (onedrive.scopes === undefined) this.$set(onedrive, 'scopes', 'offline_access User.Read Files.ReadWrite');
            if (onedrive.request_timeout_seconds === undefined) this.$set(onedrive, 'request_timeout_seconds', 60);

            const googleDrive = this.cloudProviderSettings.google_drive;
            if (googleDrive.client_id === undefined) this.$set(googleDrive, 'client_id', '');
            if (googleDrive.client_secret === undefined) this.$set(googleDrive, 'client_secret', '');
            if (googleDrive.client_secret_saved === undefined) this.$set(googleDrive, 'client_secret_saved', false);
            if (googleDrive.redirect_uri === undefined) this.$set(googleDrive, 'redirect_uri', '');
            if (googleDrive.scopes === undefined) this.$set(googleDrive, 'scopes', defaults.google_drive.scopes);
            if (googleDrive.request_timeout_seconds === undefined) this.$set(googleDrive, 'request_timeout_seconds', 60);

            const dropbox = this.cloudProviderSettings.dropbox;
            if (dropbox.app_key === undefined) this.$set(dropbox, 'app_key', '');
            if (dropbox.app_secret === undefined) this.$set(dropbox, 'app_secret', '');
            if (dropbox.app_secret_saved === undefined) this.$set(dropbox, 'app_secret_saved', false);
            if (dropbox.redirect_uri === undefined) this.$set(dropbox, 'redirect_uri', '');
            if (dropbox.scopes === undefined) this.$set(dropbox, 'scopes', defaults.dropbox.scopes);
            if (dropbox.request_timeout_seconds === undefined) this.$set(dropbox, 'request_timeout_seconds', 60);
        },
        toggleCloudHelp(key) {
            this.$set(this.cloudHelpVisible, key, !this.cloudHelpVisible[key]);
        },
        showCloudHelp(key) {
            return this.cloudHelpVisible?.[key] === true;
        },
        defaultCloudCallbackUrl(provider) {
            return `${window.location.origin}/cloud-connections/callback/${provider}`;
        },
        providerCallbackUrl(provider) {
            this.ensureCloudProviderSettings();
            const settings = this.cloudProviderSettings[provider] || {};
            return settings.redirect_uri || this.defaultCloudCallbackUrl(provider);
        },
        onedriveApplicationIdMissing() {
            this.ensureCloudProviderSettings();
            return !this.cloudProviderSettings.onedrive.client_id;
        },
        providerNeedsRemotePath(provider) {
            const selectedProvider = provider || this.indexConfigForm?.cloud?.provider;
            const definition = this.availableCloudProviders.find(item => item.id === selectedProvider);
            return definition?.requires_login === true;
        },
        providerRemotePathLabel(provider) {
            const labels = {
                onedrive: 'OneDrive folder to track',
                google_drive: 'Google Drive folder to track',
                dropbox: 'Dropbox folder to track'
            };
            return labels[provider] || 'Cloud folder to track';
        },
        providerRemotePathPlaceholder(provider) {
            const placeholders = {
                onedrive: 'Documents/PDFs',
                google_drive: 'Documents/PDFs',
                dropbox: 'Documents/PDFs'
            };
            return placeholders[provider] || 'Documents/PDFs';
        },
        providerAccountLabel(provider) {
            const labels = {
                onedrive: 'OneDrive account',
                google_drive: 'Google Drive account',
                dropbox: 'Dropbox account'
            };
            return labels[provider] || 'Cloud account';
        },
        providerConnectLabel(provider) {
            return `Connect ${this.providerLabel(provider)}`;
        },
        cloudLoginLinkLabel(provider) {
            const labels = {
                onedrive: 'Open Microsoft login',
                google_drive: 'Open Google login',
                dropbox: 'Open Dropbox login'
            };
            return labels[provider] || 'Open cloud login';
        },
        isLoginProviderConfig() {
            this.ensureIndexConfigCloudDefaults();
            return ['onedrive', 'google_drive', 'dropbox'].includes(this.indexConfigForm.cloud.provider);
        },
        providerStatusMessage(provider) {
            const selected = this.availableCloudProviders.find(item => item.id === provider);
            return selected?.missing_configuration || '';
        },
        providerSavedSecretText(provider) {
            this.ensureCloudProviderSettings();
            if (provider === 'google_drive' && this.cloudProviderSettings.google_drive.client_secret_saved) {
                return 'A Google client secret is already saved. Leave this field empty to keep it.';
            }
            if (provider === 'dropbox' && this.cloudProviderSettings.dropbox.app_secret_saved) {
                return 'A Dropbox app secret is already saved. Leave this field empty to keep it.';
            }
            return '';
        },
        applySelectedIndexConfig() {
            const config = this.indexConfigs?.[this.selectedIndex] || null;
            this.selectedIndexConfig = config;
            if (config) {
                this.indexConfigForm = this.clone(config);
            } else {
                this.indexConfigForm = this.defaultIndexConfigForm();
            }
            this.ensureIndexConfigCloudDefaults();
        },
        ensureIndexConfigCloudDefaults() {
            if (!this.indexConfigForm) {
                this.indexConfigForm = this.defaultIndexConfigForm();
            }
            if (!this.indexConfigForm.cloud) {
                this.$set(this.indexConfigForm, 'cloud', {});
            }
            const cloud = this.indexConfigForm.cloud;
            if (!cloud.provider) this.$set(cloud, 'provider', 'mounted_folder');
            if (cloud.root_path === undefined) this.$set(cloud, 'root_path', '');
            if (cloud.connection_id === undefined) this.$set(cloud, 'connection_id', '');
            if (cloud.remote_path === undefined) this.$set(cloud, 'remote_path', '');
            if (cloud.managed_root_path === undefined) this.$set(cloud, 'managed_root_path', '');
            if (cloud.display_root_name === undefined) this.$set(cloud, 'display_root_name', '');
            if (cloud.preview_folder === undefined) this.$set(cloud, 'preview_folder', '');
            if (cloud.sync_enabled === undefined) this.$set(cloud, 'sync_enabled', true);
            if (cloud.sync_interval_seconds === undefined) this.$set(cloud, 'sync_interval_seconds', 300);
            if (cloud.delete_standalone_duplicates === undefined) this.$set(cloud, 'delete_standalone_duplicates', true);
        },
        fetchIndexConfig() {
            if (!this.selectedIndex) return Promise.resolve();
            this.indexConfigLoading = true;
            return api.get('/index-config', { params: { index_name: this.selectedIndex } })
                .then(res => {
                    this.$set(this.indexConfigs, this.selectedIndex, res.data);
                    this.selectedIndexConfig = res.data;
                    this.indexConfigForm = this.clone(res.data);
                    this.ensureIndexConfigCloudDefaults();
                })
                .catch(err => {
                    console.error("Failed to load index mode settings:", err);
                })
                .finally(() => {
                    this.indexConfigLoading = false;
                });
        },
        saveIndexConfig() {
            if (!this.selectedIndex) {
                alert("Select an index first.");
                return;
            }
            if (!this.selectedIndexWritable) {
                alert("You need write access to this index to change its mode.");
                return;
            }
            if (this.selectedProviderRequiresAdmin) {
                alert("Ask an admin to configure mounted or synced host folders. You can choose OneDrive, Google Drive, or Dropbox for your own cloud account.");
                return;
            }
            this.ensureIndexConfigCloudDefaults();
            const provider = this.indexConfigForm.cloud.provider;
            if ((this.indexConfigForm.mode === 'cloud' || this.indexConfigForm.mode === 'hybrid')
                && this.providerNeedsRemotePath(provider)) {
                if (!this.indexConfigForm.cloud.connection_id) {
                    alert(`Connect or select a ${this.providerLabel(provider)} account first.`);
                    return;
                }
                if (!this.indexConfigForm.cloud.remote_path) {
                    alert(`Enter the ${this.providerLabel(provider)} folder to track.`);
                    return;
                }
            }
            this.indexConfigSaving = true;
            api.put('/index-config', {
                index_name: this.selectedIndex,
                index_config: this.indexConfigForm
            })
                .then(res => {
                    const config = res.data.index_config;
                    this.$set(this.indexConfigs, this.selectedIndex, config);
                    this.selectedIndexConfig = config;
                    this.indexConfigForm = this.clone(config);
                    if (res.data.cloud_sync_task_id) {
                        this.startTaskPolling();
                    }
                    if (this.activeTab === 'folder') {
                        this.fetchFolder('');
                    }
                    alert(res.data.message || "Index mode settings saved.");
                })
                .catch(err => {
                    alert(err.response?.data?.error || "Failed to save index mode settings.");
                })
                .finally(() => {
                    this.indexConfigSaving = false;
                });
        },
        fetchCloudProviders() {
            this.cloudProviderLoading = true;
            return api.get('/cloud-providers')
                .then(res => {
                    this.cloudProviders = res.data.providers || [];
                    this.cloudConnections = res.data.connections || [];
                    this.cloudProviderSettings = res.data.provider_settings || this.defaultCloudProviderSettings();
                    this.canManageProviderSettings = res.data.can_manage_provider_settings === true;
                    this.ensureCloudProviderSettings();
                })
                .catch(err => {
                    console.error("Failed to load cloud providers:", err);
                })
                .finally(() => {
                    this.cloudProviderLoading = false;
                });
        },
        providerLabel(providerId) {
            const provider = this.availableCloudProviders.find(item => item.id === providerId);
            return provider?.label || providerId;
        },
        selectedCloudProvider() {
            this.ensureIndexConfigCloudDefaults();
            return this.availableCloudProviders.find(item => item.id === this.indexConfigForm.cloud.provider) || null;
        },
        cloudProviderConfigured(providerId) {
            const provider = this.availableCloudProviders.find(item => item.id === providerId);
            return provider?.configured === true;
        },
        isOneDriveConfig() {
            this.ensureIndexConfigCloudDefaults();
            return this.indexConfigForm.cloud.provider === 'onedrive';
        },
        isGoogleDriveConfig() {
            this.ensureIndexConfigCloudDefaults();
            return this.indexConfigForm.cloud.provider === 'google_drive';
        },
        isDropboxConfig() {
            this.ensureIndexConfigCloudDefaults();
            return this.indexConfigForm.cloud.provider === 'dropbox';
        },
        isMountedFolderConfig() {
            this.ensureIndexConfigCloudDefaults();
            return this.indexConfigForm.cloud.provider === 'mounted_folder';
        },
        connectionOwnedByCurrentUser(connection) {
            if (this.currentUser?.is_admin) return true;
            const owner = connection.owner || {};
            if (owner.id && owner.id === this.currentUser?.id) return true;
            if (owner.login && owner.login === this.currentUser?.login) return true;
            return false;
        },
        connectionUsableForSelectedIndex(connection) {
            if (this.currentUser?.is_admin) return true;
            if (this.connectionOwnedByCurrentUser(connection)) return true;
            return this.indexConfigForm?.cloud?.connection_id === connection.id;
        },
        connectedCloudConnections(provider) {
            return (this.cloudConnections || [])
                .filter(connection => connection.provider === provider
                    && connection.status === 'connected'
                    && this.connectionUsableForSelectedIndex(connection));
        },
        cloudConnectionLabel(connection) {
            const account = connection.account || {};
            const identity = account.userPrincipalName || account.mail || account.displayName || '';
            return identity
                ? `${connection.display_name || this.providerLabel(connection.provider)} (${identity})`
                : (connection.display_name || this.providerLabel(connection.provider));
        },
        handleCloudProviderChange() {
            this.ensureIndexConfigCloudDefaults();
            const provider = this.indexConfigForm.cloud.provider;
            if (this.providerNeedsRemotePath(provider)) {
                this.indexConfigForm.cloud.root_path = '';
                this.indexConfigForm.cloud.preview_folder = '';
                if (!this.indexConfigForm.cloud.display_root_name) {
                    this.indexConfigForm.cloud.display_root_name = this.providerLabel(provider);
                }
                this.fetchCloudProviders();
                return;
            }
            this.indexConfigForm.cloud.connection_id = '';
            this.indexConfigForm.cloud.remote_path = '';
            this.indexConfigForm.cloud.managed_root_path = '';
            this.indexConfigForm.cloud.preview_folder = '';
        },
        saveCloudProviderSettings(provider) {
            if (!this.canManageProviderSettings) return;
            this.ensureCloudProviderSettings();
            const settings = this.cloudProviderSettings[provider] || {};
            if (provider === 'onedrive' && !settings.client_id) {
                alert("Enter the Microsoft application/client ID first.");
                return;
            }
            if (provider === 'google_drive' && !settings.client_id) {
                alert("Enter the Google OAuth client ID first.");
                return;
            }
            if (provider === 'dropbox' && !settings.app_key) {
                alert("Enter the Dropbox app key first.");
                return;
            }
            this.cloudProviderSettingsSaving = true;
            api.put('/cloud-provider-settings', {
                provider,
                settings
            })
                .then(res => {
                    this.cloudProviders = res.data.providers || [];
                    this.cloudConnections = res.data.connections || [];
                    this.cloudProviderSettings = res.data.provider_settings || this.defaultCloudProviderSettings();
                    this.ensureCloudProviderSettings();
                    alert(res.data.message || "Cloud provider registration saved.");
                })
                .catch(err => {
                    alert(err.response?.data?.error || "Failed to save cloud provider registration.");
                })
                .finally(() => {
                    this.cloudProviderSettingsSaving = false;
                });
        },
        startCloudConnection(provider) {
            if (!this.selectedIndexWritable) {
                alert("You need write access to this index before connecting a cloud account.");
                return;
            }
            if (!this.cloudProviderConfigured(provider)) {
                alert(`Save the ${this.providerLabel(provider)} provider registration first.`);
                return;
            }
            this.cloudConnectionStarting = true;
            api.post('/cloud-connections/start', {
                provider,
                display_name: this.providerLabel(provider),
                index_name: this.selectedIndex
            })
                .then(res => {
                    this.cloudLoginSession = res.data.connection;
                    if (this.cloudLoginSession?.authorization_url) {
                        window.open(this.cloudLoginSession.authorization_url, '_blank', 'noopener');
                    }
                    if (this.cloudLoginSession?.id) {
                        this.pollCloudConnection(this.cloudLoginSession.id);
                    }
                })
                .catch(err => {
                    alert(err.response?.data?.error || "Failed to start cloud login.");
                })
                .finally(() => {
                    this.cloudConnectionStarting = false;
                });
        },
        pollCloudConnection(connectionId) {
            this.clearCloudLoginPolling();
            const refresh = () => {
                api.get(`/cloud-connections/${connectionId}`)
                    .then(res => {
                        this.cloudLoginSession = res.data;
                        if (res.data.status === 'connected') {
                            this.clearCloudLoginPolling();
                            return this.fetchCloudProviders().then(() => {
                                this.ensureIndexConfigCloudDefaults();
                                this.indexConfigForm.cloud.connection_id = connectionId;
                                alert(`${this.providerLabel(res.data.provider)} connected.`);
                            });
                        }
                        if (res.data.status === 'failed' || res.data.status === 'expired') {
                            this.clearCloudLoginPolling();
                            alert(res.data.last_error || "Cloud login did not complete.");
                        }
                        return null;
                    })
                    .catch(err => {
                        this.clearCloudLoginPolling();
                        alert(err.response?.data?.error || "Failed to check cloud login.");
                    });
            };
            refresh();
            this.cloudLoginPollTimer = setInterval(refresh, 5000);
        },
        clearCloudLoginPolling() {
            if (this.cloudLoginPollTimer) {
                clearInterval(this.cloudLoginPollTimer);
                this.cloudLoginPollTimer = null;
            }
        },
        syncCloudNow() {
            if (!this.selectedIndex) {
                alert("Select an index first.");
                return;
            }
            if (!this.selectedIndexWritable) {
                alert("You need write access to this index to sync cloud files.");
                return;
            }
            if (this.indexConfigDirty) {
                this.saveIndexConfig();
                return;
            }
            this.cloudSyncStarting = true;
            api.post('/sync-cloud', { index_name: this.selectedIndex })
                .then(res => {
                    if (res.data.task_id) {
                        this.startTaskPolling();
                    }
                    alert(res.data.message || "Cloud sync queued.");
                })
                .catch(err => {
                    alert(err.response?.data?.error || "Failed to queue cloud sync.");
                })
                .finally(() => {
                    this.cloudSyncStarting = false;
                });
        },
        fetchFolder(path = '') {
            if (!this.selectedIndex) return Promise.resolve();
            this.folderLoading = true;
            return api.get('/folder-tree', {
                params: {
                    index_name: this.selectedIndex,
                    path
                }
            })
                .then(res => {
                    this.folderTree = res.data;
                    this.folderPath = res.data.path || '';
                })
                .catch(err => {
                    alert(err.response?.data?.error || "Failed to load folder.");
                })
                .finally(() => {
                    this.folderLoading = false;
                });
        },
        openFolder(folder) {
            this.folderPreview = null;
            return this.fetchFolder(folder.path);
        },
        openFolderPath(path) {
            this.folderPreview = null;
            return this.fetchFolder(path || '');
        },
        previewFolderFile(file, page = 'all') {
            this.folderPreview = {
                title: file.title,
                url: this.buildPreviewUrl(file, page)
            };
        },
        showSimilarFromFolder(file) {
            this.activeTab = 'search';
            this.showSimilar(file);
        },
        deleteFolderDocument(file) {
            if (!confirm(`Delete "${file.title}"? This cannot be undone.`)) return;

            api.post('/delete-document', {
                index_name: this.selectedIndex,
                id: file.id
            })
                .then(response => {
                    if (response.data.task_id) {
                        this.startTaskPolling();
                    }
                    alert(response.data.message || "Document deleted.");
                    setTimeout(() => this.fetchFolder(this.folderPath), 1000);
                })
                .catch(error => {
                    alert("Failed to delete document: " + (error.response?.data?.error || error.message));
                });
        },
        formatBytes(value) {
            const bytes = Number(value);
            if (!Number.isFinite(bytes) || bytes < 0) return 'N/A';
            if (bytes < 1024) return `${bytes} B`;
            const units = ['KB', 'MB', 'GB', 'TB'];
            let size = bytes / 1024;
            let unit = units[0];
            for (let i = 1; i < units.length && size >= 1024; i += 1) {
                size /= 1024;
                unit = units[i];
            }
            return `${size.toFixed(size >= 10 ? 1 : 2)} ${unit}`;
        },
        formatSource(source) {
            return source === 'cloud' ? 'Cloud' : 'Uploaded';
        }
    }
};
