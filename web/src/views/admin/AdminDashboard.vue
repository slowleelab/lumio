<template>
  <div class="admin-layout">
    <aside class="sidebar">
      <div class="sidebar-brand">
        <div class="brand-icon">L</div>
        <div class="brand-text">
          <span class="brand-name">Lumio</span>
          <span class="brand-sub">运营管理端</span>
        </div>
      </div>

      <el-menu
        :default-active="activeMenu"
        router
        class="sidebar-menu"
      >
        <div class="menu-section">闭环运营</div>
        <el-menu-item index="/admin/badcase">
          <el-icon><DataAnalysis /></el-icon>
          <template #title>Badcase 工作台</template>
        </el-menu-item>
        <el-menu-item index="/admin/intent-library">
          <el-icon><Notebook /></el-icon>
          <template #title>意图库管理</template>
        </el-menu-item>

        <div class="menu-section">知识库</div>
        <el-menu-item index="/admin/documents">
          <el-icon><Document /></el-icon>
          <template #title>文档管理</template>
        </el-menu-item>
        <el-menu-item index="/admin/faq">
          <el-icon><Collection /></el-icon>
          <template #title>FAQ 管理</template>
        </el-menu-item>
        <el-menu-item index="/admin/monitor">
          <el-icon><Monitor /></el-icon>
          <template #title>摄入监控</template>
        </el-menu-item>

        <div class="menu-section">质量与审计</div>
        <el-menu-item index="/admin/rag-metrics">
          <el-icon><TrendCharts /></el-icon>
          <template #title>RAG 指标</template>
        </el-menu-item>
        <el-menu-item index="/admin/audit">
          <el-icon><ChatDotRound /></el-icon>
          <template #title>对话审计</template>
        </el-menu-item>
        <el-menu-item index="/admin/ops-logs">
          <el-icon><Memo /></el-icon>
          <template #title>操作审计</template>
        </el-menu-item>
      </el-menu>

      <div class="sidebar-footer">
        <el-button text size="small" class="back-btn" @click="$router.push('/')">
          <el-icon><Back /></el-icon>
          <span>返回前台</span>
        </el-button>
      </div>
    </aside>

    <main class="content">
      <router-view v-slot="{ Component }">
        <transition name="fade" mode="out-in">
          <component :is="Component" />
        </transition>
      </router-view>
    </main>
  </div>
</template>

<script setup lang="ts">
import { computed } from "vue"
import { useRoute } from "vue-router"
import {
  Document,
  Collection,
  Monitor,
  ChatDotRound,
  Memo,
  TrendCharts,
  DataAnalysis,
  Notebook,
  Back,
} from "@element-plus/icons-vue"

const route = useRoute()
const activeMenu = computed(() => route.path)
</script>

<style scoped lang="scss">
.admin-layout {
  display: flex;
  min-height: 100vh;
  background: var(--color-bg-page, #f0f2f5);
}

.sidebar {
  width: 220px;
  min-width: 220px;
  background: #1a1b2e;
  display: flex;
  flex-direction: column;
  position: sticky;
  top: 0;
  height: 100vh;
  overflow-y: auto;
  flex-shrink: 0;

  &::-webkit-scrollbar { width: 0; }
}

.sidebar-brand {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 18px 16px 16px;

  .brand-icon {
    width: 34px;
    height: 34px;
    border-radius: 8px;
    background: linear-gradient(135deg, #409eff, #7b68ee);
    color: #fff;
    font-weight: 700;
    font-size: 18px;
    display: flex;
    align-items: center;
    justify-content: center;
    flex-shrink: 0;
  }

  .brand-text {
    display: flex;
    flex-direction: column;
  }

  .brand-name {
    font-size: 15px;
    font-weight: 600;
    color: #e8eaf0;
    line-height: 1.2;
  }

  .brand-sub {
    font-size: 11px;
    color: #7c7f92;
  }
}

.sidebar-menu {
  flex: 1;
  border-right: none;
  background: transparent;
  padding: 0 8px;

  :deep(.el-menu-item) {
    height: 38px;
    line-height: 38px;
    border-radius: 6px;
    margin-bottom: 2px;
    color: #9ca3af;
    font-size: 13px;

    .el-icon {
      color: #6b7280;
      transition: color 0.2s;
    }

    &:hover {
      background: rgba(255, 255, 255, 0.06);
      color: #d1d5db;

      .el-icon { color: #9ca3af; }
    }

    &.is-active {
      background: rgba(64, 158, 255, 0.15);
      color: #409eff;

      .el-icon { color: #409eff; }
    }
  }
}

.menu-section {
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  color: #565a6e;
  padding: 16px 12px 4px;
  user-select: none;

  &:first-of-type { padding-top: 8px; }
}

.sidebar-footer {
  margin-top: auto;
  padding: 12px 16px;
  border-top: 1px solid rgba(255, 255, 255, 0.06);

  .back-btn {
    width: 100%;
    justify-content: flex-start;
    color: #6b7280;

    &:hover { color: #9ca3af; }
  }
}

.content {
  flex: 1;
  padding: 24px 28px;
  max-width: 1440px;
  min-width: 0;
  overflow-y: auto;
}

.fade-enter-active,
.fade-leave-active {
  transition: opacity 0.15s ease;
}
.fade-enter-from,
.fade-leave-to {
  opacity: 0;
}
</style>
