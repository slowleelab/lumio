import { createRouter, createWebHistory } from "vue-router"
import CustomerChat from "@/views/CustomerChat.vue"
import AgentWorkbench from "@/views/AgentWorkbench.vue"
import LoginPage from "@/views/LoginPage.vue"
import AdminDashboard from "@/views/admin/AdminDashboard.vue"
import DocumentList from "@/views/admin/DocumentList.vue"
import FaqManager from "@/views/admin/FaqManager.vue"
import IngestionMonitor from "@/views/admin/IngestionMonitor.vue"
import AuditConversations from "@/views/admin/AuditConversations.vue"
import OperationLogs from "@/views/admin/OperationLogs.vue"
import RagMetrics from "@/views/admin/RagMetrics.vue"
import BadcaseWorkbench from "@/views/admin/BadcaseWorkbench.vue"
import IntentLibrary from "@/views/admin/IntentLibrary.vue"
import DialogueSimulator from "@/views/admin/DialogueSimulator.vue"

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: "/", name: "customer", component: CustomerChat },
    { path: "/agent", name: "agent", component: AgentWorkbench },
    { path: "/login", name: "login", component: LoginPage },
    {
      path: "/admin",
      component: AdminDashboard,
      children: [
        { path: "", redirect: "/admin/documents" },
        { path: "documents", name: "admin-documents", component: DocumentList },
        { path: "faq", name: "admin-faq", component: FaqManager },
        { path: "monitor", name: "admin-monitor", component: IngestionMonitor },
        { path: "audit", name: "admin-audit", component: AuditConversations },
        { path: "ops-logs", name: "admin-ops-logs", component: OperationLogs },
        { path: "rag-metrics", name: "admin-rag-metrics", component: RagMetrics },
        { path: "badcase", name: "admin-badcase", component: BadcaseWorkbench },
        { path: "intent-library", name: "admin-intent-library", component: IntentLibrary },
        { path: "simulator", name: "admin-simulator", component: DialogueSimulator },
      ],
    },
  ],
})

export default router
