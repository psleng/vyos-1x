<!-- include start from failover/interface-failover.xml.i -->
<tagNode name="interface">
  <properties>
    <help>Failover via a point-to-point egress interface with no gateway (e.g. cellular WWAN)</help>
    <completionHelp>
      <script>${vyos_completion_dir}/list_interfaces</script>
    </completionHelp>
    <valueHelp>
      <format>txt</format>
      <description>Egress interface name</description>
    </valueHelp>
    <constraint>
      #include <include/constraint/interface-name.xml.i>
    </constraint>
  </properties>
  #include <include/failover/common-failover.xml.i>
</tagNode>
<!-- include end -->
