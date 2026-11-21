<template>
  <section class="page" :data-module="meta.key">
    <header class="page-head">
      <div>
        <h2>{{ meta.name }}</h2>
        <p class="page-desc">{{ meta.desc }}</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">
          登记{{ meta.entity }}
        </button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">
          导出{{ meta.name }}清单
        </button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <p class="status-legend">
      <span v-for="item in statusSummary" :key="item.status" class="legend-item">
        {{ item.status }}：{{ item.count }}
      </span>
    </p>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in meta.fields" :key="column">{{ column }}</th>
          <th>当前状态</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in meta.fields" :key="column">{{ row[column] ?? '—' }}</td>
          <td>{{ row.status }}</td>
          <td class="row-actions">
            <button
              v-for="action in meta.actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
            <button
              v-if="meta.key === 'retention_job'"
              class="link"
              type="button"
              :data-testid="`report-${row.id}`"
              @click="showCleanupReport(row)"
            >
              清理对账
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="meta.fields.length + 2" class="empty-state">
            暂无{{ meta.name }}数据，可先登记{{ meta.entity }}
          </td>
        </tr>
      </tbody>
    </table>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记{{ meta.entity }}</h3>
        <label v-for="field in meta.fields" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <section v-if="meta.key === 'retention_job' && cleanupReport" class="cleanup-panel" data-testid="cleanup-report">
      <h3>清理对账 · {{ cleanupReport.asset }}</h3>
      <p class="page-desc">
        基准日 {{ cleanupReport.as_of }}；已释放 {{ cleanupReport.totals['已释放行数'] }} 行，
        暂缓留存 {{ cleanupReport.totals['留存行数'] }} 行，
        本轮可释放 {{ cleanupReport.totals['本轮可释放行数'] }} 行。
      </p>
      <table class="data-table">
        <thead>
          <tr>
            <th>分区</th><th>截止日</th><th>数据行数</th><th>已释放</th>
            <th>剩余</th><th>状态</th><th>暂缓理由</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="part in cleanupReport.partitions" :key="part.分区">
            <td>{{ part.分区 }}</td>
            <td>{{ part.截止日 }}</td>
            <td>{{ part.数据行数 }}</td>
            <td>{{ part.已释放行数 }}</td>
            <td>{{ part.剩余行数 }}</td>
            <td>{{ part.状态 }}</td>
            <td>{{ part.暂缓理由 || '—' }}</td>
          </tr>
        </tbody>
      </table>
      <p v-if="cleanupReport.active_holds.length" class="status-legend">
        <span v-for="hold in cleanupReport.active_holds" :key="hold.调查申请编号" class="legend-item">
          {{ hold.调查申请编号 }}：{{ hold.暂缓理由 }}（留存 {{ hold.留存行数 }} 行）
        </span>
      </p>
    </section>

    <footer class="page-foot">
      <span>共 {{ total }} 条{{ meta.name }}记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  createEntry,
  downloadEntries,
  listEntries,
  loadCleanupReport,
  moduleMeta,
  runAction as applyAction,
} from '@/api/backend-service'
import type { EntryRow } from '@/data/types'

type CleanupPartition = {
  分区: string
  截止日: string
  数据行数: number
  已释放行数: number
  剩余行数: number
  状态: string
  暂缓理由: string
}

type CleanupReport = {
  asset: string
  as_of: string
  partitions: CleanupPartition[]
  totals: Record<string, number>
  active_holds: { 调查申请编号: string; 暂缓理由: string; 留存行数: number }[]
}

const props = defineProps<{ moduleKey: string }>()
const meta = moduleMeta(props.moduleKey)
const rows = ref<EntryRow[]>([])
const total = ref(0)
const errorMessage = ref('')
const cleanupReport = ref<CleanupReport | null>(null)
const filters = ref<Record<string, string>>({})
const filterFields = meta.fields.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const stats = computed(() => [
  { label: meta.metrics[0], value: total.value },
  { label: meta.metrics[1], value: rows.value.filter((row) => row.status === meta.statuses[1]).length },
  {
    label: meta.metrics[2],
    value: rows.value.filter((row) => row.status === meta.statuses[meta.statuses.length - 1]).length,
  },
])
const statusSummary = computed(() =>
  meta.statuses.map((status) => ({
    status,
    count: rows.value.filter((row) => String(row.status) === status).length,
  })),
)

function resetFilters() {
  filters.value = {}
  void reload()
}

async function exportRows() {
  errorMessage.value = ''
  try {
    await downloadEntries(meta.key, filters.value)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(meta.fields.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const result = await createEntry(meta.key, createValues.value)
    if (!result.ok) throw new Error(result.message)
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

async function runAction(action: string, row: EntryRow) {
  errorMessage.value = ''
  try {
    const result = await applyAction(meta.key, Number(row.id), action)
    if (!result.ok) throw new Error(result.message)
    await reload()
    if (meta.key === 'retention_job') await showCleanupReport(row)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '操作失败'
  }
}

async function showCleanupReport(row: EntryRow) {
  try {
    cleanupReport.value = (await loadCleanupReport(meta.key, Number(row.id))) as CleanupReport
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '清理对账读取失败'
  }
}

async function reload() {
  errorMessage.value = ''
  try {
    const payload = await listEntries(meta.key, filters.value)
    rows.value = payload.items
    total.value = payload.total
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '业务列表读取失败'
  }
}

onMounted(reload)
</script>
