const SearchManager = {
    data() {
        return {
            query: '',
            results: [],
            searchSubmitted: false,
            previewUrl: null,
            selectedPages: '',
            searchMode: 'keyword',
            similaritySourceTitle: '',
            similarityLoading: false,
            similarityUploadFile: null,
            similarityUploadVisible: false,
            activeAppendKey: null,
            appendFiles: {},
            lowDataMode: (() => {
                try {
                    return localStorage.getItem('pdfSearchLowDataMode') === 'true';
                } catch (error) {
                    return false;
                }
            })()
        };
    },
    watch: {
        lowDataMode(enabled) {
            try {
                localStorage.setItem('pdfSearchLowDataMode', enabled ? 'true' : 'false');
            } catch (error) {
                // Ignore storage failures; previews still work for the current session.
            }
            this.refreshVisiblePreviews();
        }
    },
    methods: {
        normalizeResult(result) {
            return {
                ...result,
                previewVisible: false,
                previewMode: '',
                previewPageNumber: null,
                selectedPages: ''
            };
        },
        buildPreviewUrl(result, page) {
            const params = new URLSearchParams({
                index_name: this.selectedIndex,
                page: String(page)
            });
            if (result.id) params.set('id', result.id);
            if (result.path) params.set('path', result.path);

            if (this.lowDataMode) {
                params.set('low_data', '1');
            }

            return `${api.defaults.baseURL}/preview?${params.toString()}`;
        },
        refreshVisiblePreviews() {
            this.results.forEach(result => {
                if (!result.previewVisible) return;

                if (result.previewMode === 'pdf') {
                    result.previewUrl = this.buildPreviewUrl(result, 'all');
                } else if (result.previewPageNumber) {
                    result.previewUrl = this.buildPreviewUrl(result, result.previewPageNumber);
                }
            });
        },
        search() {
            if (!this.selectedIndex) {
                alert("Please select an index to search.");
                return;
            }
            this.searchSubmitted = true; // Mark search as submitted
            this.searchMode = 'keyword';
            this.similaritySourceTitle = '';
            this.similarityUploadVisible = false;
            this.similarityUploadFile = null;
            api.get('/search', {
                params: { q: this.query, index_name: this.selectedIndex }
            })
                .then(response => {
                    if (Array.isArray(response.data) && response.data.length > 0) {
                        this.results = response.data.map(result => this.normalizeResult(result));
                    } else {
                        this.results = [];
                    }
                })
                .catch(error => {
                    console.error("Search failed:", error.response || error.message);
                    alert("An error occurred while searching. Please try again.");
                });
        },
        previewPage(result, index) {
            // Hide other previews
            this.results.forEach((res, idx) => {
                if (idx !== index) res.previewVisible = false;
            });

            // If already visible, toggle off
            if (result.previewVisible) {
                result.previewVisible = false;
                return;
            }

            result.previewMode = 'page';

            if (this.searchMode === 'similar') {
                result.previewPageNumber = 1;
                result.previewUrl = this.buildPreviewUrl(result, 1);
                result.previewVisible = true;
                return;
            }

            // Set loading state here if you want a spinner later
            api.get('/find-hit-page', {
                params: {
                    index_name: this.selectedIndex,
                    id: result.id,
                    path: result.path,
                    q: this.query
                }
            }).then(response => {
                const page = response.data.page;
                if (page && page > 0) {
                    result.previewPageNumber = page;
                    result.previewUrl = this.buildPreviewUrl(result, page);
                    result.previewVisible = true;  // ✅ only now show it
                } else {
                    alert("Keyword not found in document.");
                }
            }).catch(err => {
                alert("Error determining keyword hit page.");
            });
        },
        previewPdf(result, index) {
            // Hide other previews if one is already visible
            this.results.forEach((res, idx) => {
                if (idx !== index) res.previewVisible = false;
            });

            // Toggle visibility and set preview URL for the selected result
            result.previewVisible = !result.previewVisible;
            if (result.previewVisible) {
                result.previewMode = 'pdf';
                result.previewPageNumber = null;
                result.previewUrl = this.buildPreviewUrl(result, 'all');
            }
        },
        handleSimilarityFileUpload(event) {
            this.similarityUploadFile = event.target.files[0] || null;
        },
        toggleSimilarityUpload() {
            this.similarityUploadVisible = !this.similarityUploadVisible;
            if (!this.similarityUploadVisible) {
                this.similarityUploadFile = null;
            }
        },
        cancelSimilarityUpload() {
            this.similarityUploadVisible = false;
            this.similarityUploadFile = null;
        },
        appendFileKey(result) {
            return result.id || result.path;
        },
        isAppendControlVisible(result) {
            return this.activeAppendKey === this.appendFileKey(result);
        },
        toggleAppendControl(result) {
            const key = this.appendFileKey(result);
            if (this.activeAppendKey === key) {
                this.activeAppendKey = null;
                this.$delete(this.appendFiles, key);
                return;
            }
            this.activeAppendKey = key;
        },
        cancelAppendControl(result) {
            const key = this.appendFileKey(result);
            this.activeAppendKey = null;
            this.$delete(this.appendFiles, key);
        },
        handleAppendFileUpload(result, event) {
            this.$set(this.appendFiles, this.appendFileKey(result), event.target.files[0] || null);
        },
        appendDocument(result) {
            if (!this.selectedIndex) {
                alert("Please select an index first.");
                return;
            }
            if (!result.id) {
                alert("This document cannot be appended because it has no document id.");
                return;
            }

            const key = this.appendFileKey(result);
            const appendFile = this.appendFiles[key];
            if (!appendFile) {
                alert("Please choose a PDF to append.");
                return;
            }
            if (!appendFile.name.toLowerCase().endsWith('.pdf')) {
                alert("Only PDF files can be appended.");
                return;
            }
            if (!confirm(`Append "${appendFile.name}" to "${result.title}"?`)) return;

            const formData = new FormData();
            formData.append("index_name", this.selectedIndex);
            formData.append("id", result.id);
            formData.append("file", appendFile);

            this.loading = true;

            api.post('/append-document', formData, {
                headers: { 'Content-Type': 'multipart/form-data' }
            })
                .then(response => {
                    if (response.data.task_id) {
                        this.startTaskPolling();
                    }
                    this.$delete(this.appendFiles, key);
                    this.activeAppendKey = null;
                    alert(response.data.message || "Document append started.");
                })
                .catch(error => {
                    alert("Failed to append document: " + (error.response?.data?.error || error.message));
                })
                .finally(() => {
                    this.loading = false;
                });
        },
        showSimilar(result) {
            if (!this.selectedIndex) {
                alert("Please select an index first.");
                return;
            }

            this.searchSubmitted = true;
            this.searchMode = 'similar';
            this.similaritySourceTitle = result.title;
            this.similarityLoading = true;
            this.results = [];

            api.get('/similar-documents', {
                params: {
                    index_name: this.selectedIndex,
                    id: result.id
                }
            })
                .then(response => {
                    const similarResults = Array.isArray(response.data.results)
                        ? response.data.results
                        : [];
                    this.similaritySourceTitle = response.data.title || result.title;
                    this.results = similarResults.map(similar => this.normalizeResult(similar));
                })
                .catch(error => {
                    console.error("Similar document search failed:", error.response || error.message);
                    alert(error.response?.data?.error || "Failed to find similar documents.");
                    this.searchMode = 'keyword';
                    this.similaritySourceTitle = '';
                })
                .finally(() => {
                    this.similarityLoading = false;
                });
        },
        searchSimilarFromFile() {
            if (!this.selectedIndex) {
                alert("Please select an index first.");
                return;
            }
            if (!this.similarityUploadFile) {
                alert("Please choose a PDF first.");
                return;
            }

            const formData = new FormData();
            formData.append("index_name", this.selectedIndex);
            formData.append("file", this.similarityUploadFile);
            const sourceTitle = this.similarityUploadFile.name;

            this.searchSubmitted = true;
            this.searchMode = 'similar';
            this.similaritySourceTitle = sourceTitle;
            this.similarityLoading = true;
            this.results = [];

            api.post('/similar-documents-from-file', formData, {
                headers: { 'Content-Type': 'multipart/form-data' }
            })
                .then(response => {
                    const similarResults = Array.isArray(response.data.results)
                        ? response.data.results
                        : [];
                    this.similaritySourceTitle = response.data.title || sourceTitle;
                    this.results = similarResults.map(result => this.normalizeResult(result));
                    this.similarityUploadVisible = false;
                    this.similarityUploadFile = null;
                })
                .catch(error => {
                    console.error("PDF similarity search failed:", error.response || error.message);
                    alert(error.response?.data?.error || "Failed to search similar documents for this PDF.");
                    this.searchMode = 'keyword';
                    this.similaritySourceTitle = '';
                })
                .finally(() => {
                    this.similarityLoading = false;
                });
        },
        exitPreview() {
            this.previewUrl = null;
        },
        download(target) {
            const isResult = target && typeof target === 'object';
            const payload = isResult
                ? { index_name: this.selectedIndex, id: target.id, path: target.path }
                : { path: target };
            const filenameSource = isResult ? (target.title || target.path || 'document.pdf') : target;

            api.post('/download', payload, { responseType: 'blob' })
                .then(response => {
                    const filename = filenameSource.split('/').pop();
                    const url = window.URL.createObjectURL(new Blob([response.data]));
                    const link = document.createElement('a');
                    link.href = url;
                    link.setAttribute('download', filename);
                    document.body.appendChild(link);
                    link.click();
                });
        },
        downloadSelectedPages(result) {
            if (!result.selectedPages) {
                alert("Please enter the page numbers to download.");
                return;
            }

            const pages = result.selectedPages.split(',').map(Number).filter(n => !isNaN(n));

            if (pages.length === 0) {
                alert("Invalid page numbers. Please try again.");
                return;
            }

            api.post('/download', {
                index_name: this.selectedIndex,
                id: result.id,
                path: result.path,
                pages: pages
            }, { responseType: 'blob' })
                .then(response => {
                    const filename = `selected_pages_${result.path.split('/').pop()}`;
                    const url = window.URL.createObjectURL(new Blob([response.data]));
                    const link = document.createElement('a');
                    link.href = url;
                    link.setAttribute('download', filename);
                    document.body.appendChild(link);
                    link.click();
                })
                .catch(error => alert("Failed to download selected pages: " + error.message));
        },
        deleteDocument(result) {
            if (!confirm(`Delete "${result.title}"? This cannot be undone.`)) return;

            api.post('/delete-document', {
                index_name: this.selectedIndex,
                id: result.id  // must be returned in the search result
            })
                .then(response => {
                    if (response.data.task_id) {
                        this.startTaskPolling();
                    }
                    alert(response.data.message || "Document deleted.");
                    if (this.searchMode === 'similar') {
                        this.results = this.results.filter(item => item.id !== result.id);
                    } else if (this.activeTab === 'folder') {
                        this.fetchFolder(this.folderPath || '');
                    } else {
                        // Refresh search results
                        this.search();
                    }
                })
                .catch(error => {
                    alert("Failed to delete document: " + (error.response?.data?.error || error.message));
                });
        },
        downloadAll() {
            this.results.forEach(result => this.download(result));
        }
    }
};
