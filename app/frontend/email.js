




const EmailManager = {
    data() {
        return {
            emailTasks: [],
            emailForm: {
                email: '',
                password: '',
                imap_server: '',
                imap_port: 993
            }
        };
    },
    computed: {

    },
    methods: {
        // Fetch running email monitor tasks for selected index
        fetchEmailTasks() {
            if (!this.selectedIndex) return;
            api.get(`/email-tasks?index_name=${this.selectedIndex}`)
                .then(res => this.emailTasks = res.data.tasks || [])
                .catch(err => console.error("Failed to load email tasks", err));
        },



        createEmailTask(mode) {
            // Validate required fields
            const requiredFields = ['email', 'password', 'imap_server', 'imap_port'];
            for (const field of requiredFields) {
                if (!this.emailForm[field]) {
                    alert(`Please fill out the ${field.replace('_', ' ')} field.`);
                    return;
                }
            }

            if (!this.selectedIndex) {
                alert("Please select an index.");
                return;
            }




            const payload = {
                index_name: this.selectedIndex,
                mode: mode,
                ...this.emailForm  // Spread the form fields directly into the root object
            };

            api.post('/email-task', payload)
                .then(res => {
                    alert(res.data.message);
                    this.fetchEmailTasks();
                })
                .catch(err => {
                    alert(err.response?.data?.error || "Failed to start task");
                });
        },

        stopEmailTask(taskId) {
            const confirmed = confirm(`Stop task ${taskId}?`);
            if (!confirmed) return;

            api.post('/stop-email-task', { task_id: taskId, index_name: this.selectedIndex })
                .then(res => {
                    alert(res.data.message);
                    this.fetchEmailTasks();
                })
                .catch(err => {
                    console.error("Failed to stop email task", err);
                    alert(err.response?.data?.error || "Error stopping task");
                });
        }
    }
};
