## 1. 排版

**粗体** _斜体_

~~这是一段错误的文本。~~

引用:

> 引用 Leanote 官方的话, 为什么要做 Leanote, 原因是...
>
> > a nested blq
> >
> > > nested nested blq
>
> awa

有序列表:

1. 支持 Vim
2. 支持 Emacs

无序列表:

- 项目 1
- 项目 2

## 2. 图片与链接

图片:
![leanote](http://leanote.com/images/logo/leanote_icon_blue.png)
链接:

[外部链接](https://baidu.com/)

## 3. 标题

以下是各级标题, 最多支持 5 级标题

```
# h1
## h2
### h3
#### h4
##### h4
###### h5
```

## 4. 代码

示例:

    function get(key) {
        return m[key];
    }

代码高亮示例:

```javascript
/**
 * nth element in the fibonacci series.
 * @param n >= 0
 * @return the nth element, >= 0.
 */
function fib(n) {
  var a = 1,
    b = 1;
  var tmp;
  while (--n >= 0) {
    tmp = a;
    a += b;
    b = tmp;
  }
  return a;
}

document.write(fib(10)).awa.pwp.qwq.dmd.ovo.igvhdvhmvzdsczhgdvhzhsvcdgv;
```

```python
class Employee:
    empCount = 0

    def __init__(self, name, salary):
        self.name = name
        self.salary = salary
        Employee.empCount += 1
```

# 5. Markdown 扩展

Markdown 扩展支持:

- 表格
- 定义型列表
- Html 标签
- 脚注
- 目录
- 时序图与流程图
- MathJax 公式

## 5.1 表格

| Item     | Value  |
| -------- | ------ |
| Computer | \$1600 |
| Phone    | \$12   |
| Pipe     | \$1    |

可以指定对齐方式, 如 Item 列左对齐, Value 列右对齐, Qty 列居中对齐

| Item     |  Value | Qty |
| :------- | -----: | :-: |
| Computer | \$1600 |  5  |
| Phone    |   \$12 | 12  |
| Pipe     |    \$1 | 234 |

## 5.2 定义型列表

<details>
    <summary>名词 1</summary>

```js
nm$l.launch(/nmmsl/g);
```

</details>

## 5.3 Html 标签

支持在 Markdown 语法中嵌套 Html 标签，譬如，你可以用 Html 写一个纵跨两行的表格：

    <table>
        <tr>
            <th rowspan="2">值班人员</th>
            <th>星期一</th>
            <th>星期二</th>
            <th>星期三</th>
        </tr>
        <tr>
            <td>李强</td>
            <td>张明</td>
            <td>王平</td>
        </tr>
    </table>

<table>
    <tr>
        <th rowspan="2">值班人员</th>
        <th>星期一</th>
        <th>星期二</th>
        <th>星期三</th>
    </tr>
    <tr>
        <td>李强</td>
        <td>张明</td>
        <td>王平</td>
    </tr>
</table>

## 5.4 脚注

Leanote[^footnote]来创建一个脚注

[^footnote]: Leanote 是一款强大的开源云笔记产品.

## 5.5 目录

通过 `[TOC]` 在文档中插入目录, 如:

[TOC]

## 5.6 时序图与流程图

```sequence
Alice->Bob: Hello Bob, how are you?
Note right of Bob: Bob thinks
Bob-->Alice: I am good thanks!
```

流程图:

```flow
st=>start: Start
e=>end
op=>operation: My Operation
cond=>condition: Yes or No?

st->op->cond
cond(yes)->e
cond(no)->op
```

> **提示:** 更多关于时序图与流程图的语法请参考:
>
> - [时序图语法](http://bramp.github.io/js-sequence-diagrams/)
> - [流程图语法](http://adrai.github.io/flowchart.js)

## 5.7 MathJax 公式

`$`表示行内公式：

质能守恒方程可以用一个很简洁的方程式 $E=mc^2$ 来表达。

`$$`表示整行公式：

$$ \sum^b_a \phi(x) $$

$$ \sum $$
