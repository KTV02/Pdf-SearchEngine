const UploadManager = {
    data() {
        return {
            file: null
        };
    },
    methods: {
        handleFileUpload(e) {
            this.file = e.target.files[0];
        },
        uploadDocument() {
            if (!this.selectedIndex) {
                alert("Please select an index to upload the document.");
                return;
            }

            if (!this.uploadsEnabled) {
                alert("Uploads are disabled for cloud-only indices. Switch to hybrid mode first.");
                return;
            }

            if (!this.file) {
                alert("Please select a file before uploading.");
                return;
            }

            const formData = new FormData();
            formData.append("file", this.file);
            formData.append("index_name", this.selectedIndex);

            this.loading = true;

            api.post('/add-document', formData, {
                headers: { "Content-Type": "multipart/form-data" }
            })
                .then(response => {
                    if (response.data.task_id) {
                        this.startTaskPolling();
                    }
                    const data = response.data;

                    // Handle ZIP response
                    if (data.duplicates_skipped !== undefined || data.failed_files !== undefined) {
                        let message = `${data.message}\n`;

                        if (data.duplicates_skipped)
                            message += `Duplicates skipped: ${data.duplicates_skipped}\n`;

                        if (data.failed_files?.length)
                            message += `Failed files: ${data.failed_files.join(', ')}\n`;

                        alert(message.trim());
                    } else {
                        // Single file response
                        alert(data.message || "Document added and indexed successfully.");
                    }
                })
                .catch(error => {
                    alert("Failed to upload document: " + (error.response?.data?.error || error.message));
                })
                .finally(() => {
                    this.loading = false;
                    this.file = null;
                });
        }
    }
};
