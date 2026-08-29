/**
 * 轻量 ECharts 组合式封装
 * 按需注册（核心 + Line/Bar/Pie），容器尺寸变化自动 resize，卸载自动销毁
 */
import { onBeforeUnmount, onMounted, watch, type Ref } from "vue"
import * as echarts from "echarts/core"
import { BarChart, LineChart, PieChart } from "echarts/charts"
import {
  DataZoomComponent,
  GridComponent,
  LegendComponent,
  TitleComponent,
  TooltipComponent,
} from "echarts/components"
import { CanvasRenderer } from "echarts/renderers"
import type { EChartsCoreOption } from "echarts/core"

echarts.use([
  BarChart,
  LineChart,
  PieChart,
  GridComponent,
  LegendComponent,
  TitleComponent,
  TooltipComponent,
  DataZoomComponent,
  CanvasRenderer,
])

export function useChart(el: Ref<HTMLElement | null>, options: Ref<EChartsCoreOption | null>) {
  let chart: echarts.ECharts | null = null
  let observer: ResizeObserver | null = null

  function render() {
    if (chart && options.value) {
      chart.setOption(options.value, { notMerge: true })
    }
  }

  onMounted(() => {
    if (!el.value) return
    chart = echarts.init(el.value)
    render()
    observer = new ResizeObserver(() => chart?.resize())
    observer.observe(el.value)
  })

  watch(options, render, { deep: true })

  onBeforeUnmount(() => {
    observer?.disconnect()
    chart?.dispose()
    chart = null
  })

  return { el }
}
