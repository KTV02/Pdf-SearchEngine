const TaskManager = {
    data() {
        return {
            currentTaskId: null,
            currentTaskStatus: null,
            taskPollingInterval: null,
            taskDetailsVisible: {},
            taskLogMinimized: false,
            coordinatorStatus: null,
            handledAppendTaskIds: {},
            handledCloudTaskIds: {}
        };
    },
    methods: {
        startTaskPolling() {
            if (this.taskPollingInterval) return; // Avoid multiple intervals

            this.fetchTaskStatus(); // Fetch immediately
            this.taskPollingInterval = setInterval(() => {
                this.fetchTaskStatus();
            }, 5000); // Fetch every 2 seconds (or adjust as needed)
        },
        stopTaskPolling() {
            if (this.taskPollingInterval) {
                clearInterval(this.taskPollingInterval);
                this.taskPollingInterval = null;
            }
            this.currentTaskStatus = null;
            this.coordinatorStatus = null;
        },
        taskTimestamp(task) {
            return Date.parse(task.updated_at || task.created_at || 0) || 0;
        },
        coordinatorStatusClass() {
            const status = this.coordinatorStatus?.status;
            if (!status?.available) return "warn";
            if (status.restricted) return "blocked";
            return "ok";
        },
        coordinatorStatusLabel() {
            if (!this.coordinatorStatus?.enabled) return "";
            const status = this.coordinatorStatus.status || {};
            if (!status.available) return "unavailable";
            if (status.restricted) return "heavy jobs locked";
            return "available";
        },
        coordinatorPausedCount() {
            return Object.keys(this.coordinatorStatus?.paused_tasks || {}).length;
        },
        formatDuration(seconds) {
            const value = Number(seconds);
            if (!Number.isFinite(value) || value < 0) return "";
            if (value < 60) return `${Math.round(value)}s`;
            const minutes = Math.floor(value / 60);
            const remainingSeconds = Math.round(value % 60);
            if (minutes < 60) return remainingSeconds ? `${minutes}m ${remainingSeconds}s` : `${minutes}m`;
            const hours = Math.floor(minutes / 60);
            const remainingMinutes = minutes % 60;
            return remainingMinutes ? `${hours}h ${remainingMinutes}m` : `${hours}h`;
        },
        normalizeTaskList(tasksPayload) {
            const entries = Array.isArray(tasksPayload)
                ? tasksPayload.map(task => [task.id, task])
                : Object.entries(tasksPayload || {});

            return entries
                .map(([taskId, task]) => ({ ...task, id: task.id || taskId }))
                .sort((a, b) => this.taskTimestamp(b) - this.taskTimestamp(a))
                .slice(0, 3);
        },
        toggleTaskDetails(taskId) {
            this.$set(this.taskDetailsVisible, taskId, !this.taskDetailsVisible[taskId]);
        },
        isTaskDetailsVisible(taskId) {
            return !!this.taskDetailsVisible[taskId];
        },
        toggleTaskLog() {
            this.taskLogMinimized = !this.taskLogMinimized;
        },
        formatTaskDetails(task) {
            if (!task.result) return "No additional details reported yet.";
            return JSON.stringify(task.result, null, 2);
        },
        syncCompletedAppendTask(task) {
            const result = task.result || {};
            if (task.status !== "done" || result.action !== "append") return;
            if (this.handledAppendTaskIds[task.id]) return;

            if (Array.isArray(this.results)) {
                const appendedResult = this.results.find(item =>
                    item.id === result.old_id || item.path === result.path
                );
                if (appendedResult) {
                    appendedResult.id = result.new_id || appendedResult.id;
                    appendedResult.path = result.path || appendedResult.path;
                    appendedResult.title = result.appended_to || appendedResult.title;
                }
            }

            this.$set(this.handledAppendTaskIds, task.id, true);
        },
        syncCompletedCloudTask(task) {
            const result = task.result || {};
            if (task.status !== "done" || !result.message?.startsWith("Cloud sync complete")) return;
            if (this.handledCloudTaskIds[task.id]) return;
            this.$set(this.handledCloudTaskIds, task.id, true);
            this.fetchIndexConfig();
            if (this.activeTab === 'folder') {
                this.fetchFolder(this.folderPath || '');
            }
        },
        async fetchTaskStatus() {
            if (!this.selectedIndex) return;

            try {
                this.fetchCoordinatorStatus();
                api.get('/tasks', { params: { index_name: this.selectedIndex } })
                    .then(res => {
                        const tasks = this.normalizeTaskList(res.data);
                        this.currentTaskStatus = tasks.length ? tasks : null;
                        tasks.forEach(task => this.syncCompletedAppendTask(task));
                        tasks.forEach(task => this.syncCompletedCloudTask(task));
                    })
                    .catch(err => {
                        console.error("Failed to fetch task status:", err);
                    });
            } catch (err) {
                console.error("Fetch error:", err);
            }
        },
        async fetchCoordinatorStatus() {
            if (!this.selectedIndex) return;

            api.get('/job-coordinator/status', { params: { index_name: this.selectedIndex } })
                .then(res => {
                    this.coordinatorStatus = res.data;
                })
                .catch(err => {
                    if (err.response?.status !== 404) {
                        console.error("Failed to fetch coordinator status:", err);
                    }
                    this.coordinatorStatus = null;
                });
        }
    }
};
