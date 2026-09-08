<!-- include start from serial/service/modbus-not-profileable.xml.i -->
<node name="modbus-gateway">
  <children>
    <node name="slave">
      <children>
        #include <include/serial/service/utils/aliasing-address.xml.i>
        <leafNode name="uid">
          <properties>
            <help>Slave UID or UID range</help>
            <valueHelp>
              <format>start-end</format>
              <description>UID range (e.g. 2-5) to match</description>
            </valueHelp>
            <valueHelp>
              <format>&lt;1-247&gt;</format>
              <description>UID number, from 1 to 247</description>
            </valueHelp>
            <constraint>
              <validator name="modbus-uid-range"/>
            </constraint>
          </properties>
        </leafNode>
      </children>
    </node>
  </children>
</node>
<!-- include end -->
